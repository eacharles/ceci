import parsl
from parsl.data_provider.files import File
from .stage import PipelineStage
from . import minirunner
import os
import sys
import time


class StageExecutionConfig:
    def __init__(self, info):
        self.name = info['name']
        self.nprocess = info.get('nprocess', 1)
        self.nodes = info.get('nodes', 1)
        self.threads_per_process = info.get('threads_per_process', 1) #
        self.mem_per_process = info.get('mem_per_process', 2)
        self.shifter = info.get('shifter')
        self.docker = info.get('docker')
        #TODO assign sites better
        self.executor = info['site']
        self.mpi_command = info['mpi_command']

    def generate_launcher(self):
        nprocess = self.nprocess
        nthread = self.threads_per_process
        mpi_command = self.mpi_command
        shifter_image = self.shifter
        docker_image = self.docker
        nodes = self.nodes

        if shifter_image and docker_image:
            raise ValueError("Cannot use both shifter and docker")

        if docker_image:
            raise ValueError("Docker is not yet supported, just shifter")
        if shifter_image:
            shifter_cmd = f"shifter --env OMP_NUM_THREADS={nthread} --image={shifter_image}"
            if nthread:
                shifter_cmd
        else:
            shifter_cmd = ""

        # This is identical to the parsl case however
        if nprocess > 1:
            pre_command = f"OMP_NUM_THREADS={nthread} {mpi_command} {nprocess} -N {nodes} {shifter_cmd}"
            post_command = "--mpi"
        else:
            if shifter_image:
                pre_command = shifter_cmd
            else:
                pre_command = f"OMP_NUM_THREADS={nthread}"
            post_command = ""


        if docker_image:            
            pre_command = f'docker run -v $PWD:/opt/txpipe --env OMP_NUM_THREADS={nthread} --rm -it {docker_image} {pre_command}'

        return pre_command, post_command

class Pipeline:
    def __init__(self, stages):
        self.stage_execution_config = {}
        self.stage_names = []
        for info in stages:
            self.add_stage(info)

    def add_stage(self, stage_info):
        sec = StageExecutionConfig(stage_info)
        self.stage_execution_config[sec.name] = sec
        self.stage_names.append(sec.name)

    def remove_stage(self, name):
        self.stage_names.remove(name)
        del self.stage_execution_config[name]

    def find_outputs(self, stage, outdir):
        return [f'{outdir}/{ftype.make_name(tag)}' for tag,ftype in stage.outputs]

    def find_inputs(self, stage, data_elements):
        inputs = []
        for inp in stage.input_tags():
            item = data_elements[inp]
            if isinstance(item,str):
                item = File(item)
            inputs.append(item)
        return inputs


    def ordered_stages(self, overall_inputs):
        stage_names = self.stage_names[:]
        stages = [PipelineStage.get_stage(stage_name) for stage_name in stage_names]
        all_stages = stages[:]
        known_inputs = list(overall_inputs.keys())
        ordered_stages = []
        n = len(stage_names)

        for i in range(n):
            for stage in stages[:]:
                if all(inp in known_inputs for inp in stage.input_tags()):
                    ordered_stages.append(stage)
                    known_inputs += stage.output_tags()
                    stages.remove(stage)

        if stages:
            all_outputs = sum((s.output_tags() for s in all_stages), [])
            missing_inputs = [t for s in all_stages for t in s.input_tags() if t not in all_outputs and t not in overall_inputs]

            if not missing_inputs:
                msg = """
                The pipeline you have written is circular!

                (Some outputs from the overall pipeline are also inputs to it.)
                """
                raise ValueError(msg)
            else:
                stages_requiring_missing_inputs = {
                    m: ", ".join([s.name for s in all_stages if m in s.input_tags()])
                    for m in missing_inputs}


                msg = f"""
                Some required inputs to the pipeline could not be found,
                (or possibly your pipeline is cyclic).

                These inputs are never generated or specified:
                {missing_inputs}

                They are needed by these stages:
                {stages_requiring_missing_inputs}
                """
            raise ValueError(msg)
        return ordered_stages

    def dry_run(self, overall_inputs, output_dir, stages_config):
        stages = self.ordered_stages(overall_inputs)

        for stage in stages:
            sec = self.stage_execution_config[stage.name]
            cmd = stage.generate_command(overall_inputs, stages_config, output_dir, sec)
            print(cmd)
            print()

    def build_mini_dag(self, stages, jobs):
        depend = {}
        for stage in stages[:]:
            depend[jobs[stage.name]] = []
            job = jobs[stage.name]
            for tag in stage.input_tags():
                for potential_parent in stages[:]:
                    if tag in potential_parent.output_tags():
                        depend[job].append(jobs[potential_parent.name])
        return depend

    def build_mini_jobs(self, stages, overall_inputs, stages_config, output_dir):
        jobs = {}
        for stage in stages:
            sec = self.stage_execution_config[stage.name]
            cmd = stage.generate_command(overall_inputs, stages_config, output_dir, sec)
            jobs[stage.name] = minirunner.Job(stage.name, cmd, 
                cores=sec.threads_per_process*sec.nprocess, nodes=sec.nodes)
        return jobs



    def mini_run(self, overall_inputs, output_dir, log_dir, resume, stages_config, interval=10):
        # run using minirunner instead of parsl

        # We just run this to check that the pipeline is val
        self.ordered_stages(overall_inputs)

        stages = [PipelineStage.get_stage(stage_name) for stage_name in self.stage_names]
        jobs = self.build_mini_jobs(stages, overall_inputs, stages_config, output_dir)
        graph = self.build_mini_dag(stages, jobs)
        nodes = minirunner.build_node_list()
        runner = minirunner.Runner(nodes, graph, log_dir)
        status = minirunner.WAITING
        while status == minirunner.WAITING:
            status = runner.update()
            try:
                time.sleep(interval)
            except KeyboardInterrupt:
                runner.abort()
                raise



    def run(self, overall_inputs, output_dir, log_dir, resume, stages_config):
        stages = self.ordered_stages(overall_inputs)
        data_elements = overall_inputs.copy()
        futures = []

        os.makedirs(output_dir, exist_ok=True)
        os.makedirs(log_dir, exist_ok=True)

        if resume:
            print("Since parameter 'resume' is True we will skip steps whose outputs exist already")

        for stage in stages:
            sec = self.stage_execution_config[stage.name]
            app = stage.generate_parsl_app(log_dir, sec)
            inputs = self.find_inputs(stage, data_elements)
            outputs = self.find_outputs(stage, output_dir)
            # All pipeline stages implicitly get the overall configuration file
            inputs.append(File(stages_config))
            already_run_stage = all(os.path.exists(output) for output in outputs)
            # If we are in "resume" mode and the pipeline has already been run
            # then we re-use any existing outputs.  User is responsible for making
            # sure they are complete!
            if resume and already_run_stage:
                print(f"Skipping stage {stage.name} because its outputs exist already")
                for (tag,_),filename in zip(stage.outputs, outputs):
                    data_elements[tag] = filename
            # Otherwise, run the pipeline and register any outputs from the
            # pipe element as a "future" - a file that the pipeline will
            # create later
            else:
                print(f"Pipeline queuing stage {stage.name} with {sec.nprocess} processes")
                future = app(inputs=inputs, outputs=outputs)
                future._ceci_name = stage.name
                futures.append(future)
                for i, output in enumerate(stage.output_tags()):
                    data_elements[output] = future.outputs[i]

        # Wait for the final results, from all files
        for future in futures:
            try:
                future.result()
            except parsl.app.errors.AppFailure:
                stdout_file = f'{log_dir}/{future._ceci_name}.err'
                stderr_file = f'{log_dir}/{future._ceci_name}.out'
                sys.stderr.write(f"""
*************************************************
Error running pipeline stage {future._ceci_name}.

Standard output and error streams below.

*************************************************

Standard output:
----------------

""")
                if os.path.exists(stdout_file):
                    sys.stderr.write(open(stdout_file).read())
                else:
                    sys.stderr.write("STDOUT MISSING!\n\n")

                sys.stderr.write(f"""
*************************************************

Standard error:
----------------

""")


                if os.path.exists(stderr_file):
                    sys.stderr.write(open(stderr_file).read())
                else:
                    sys.stderr.write("STDERR MISSING!\n\n")
                print("Pipeline failed.  No joy sparked.")
                return None

        # Return a dictionary of the resulting file outputs
        print("Pipeline suceeded.  Joy is sparked.  ")
        return data_elements

    def generate_cwl(self, overall_inputs):
        """
        Exports the pipeline as a CWL object
        """
        import cwlgen
        import cwlgen.workflow
        wf = cwlgen.workflow.Workflow()

        # List all the workflow steps and order them
        stages = self.ordered_stages(overall_inputs)

        known_outputs ={}
        workflow_inputs= {}

        cwl_steps = []
        for stage in stages:
            # Get the CWL tool for that stage
            cwl_tool = stage.generate_cwl()

            # Loop over the inputs of the tool
            cwl_inputs = []
            for inp in cwl_tool.inputs:

                # Check if we have encountered this parameter before
                if inp.id in known_outputs:
                    src = known_outputs[inp.id]+'/'+inp.id
                else:
                    # If we haven't seen that parameter before, first check if
                    # it's an option, in which case gave it a special name so
                    # that it's not confused with another pipeline stage
                    if inp.id in stage.config_options:
                        src = f'{inp.id}@{cwl_tool.id}'
                        workflow_inputs[src] = inp
                    else:
                        # Otherwise, treat it as a shared pipeline input
                        src = inp.id
                        if src not in workflow_inputs:
                            workflow_inputs[src] = inp

                cwl_inputs.append(cwlgen.workflow.WorkflowStepInput(id=inp.id, src=src))


            cwl_outputs = []
            for o in stage.outputs:
                cwl_outputs.append(o[0])

            step = cwlgen.workflow.WorkflowStep(stage.name,
                                       inputs=cwl_inputs,
                                       outputs=cwl_outputs,
                                       run='%s.cwl'%cwl_tool.id)

            # Keeping track of known output providers
            for o in stage.outputs:
                known_outputs[o[0]] = step.id

            cwl_steps.append(step)

        wf.steps = cwl_steps

        # Export the inputs of the workflow
        for inp in workflow_inputs:
            cwl_inp = cwlgen.workflow.InputParameter(inp,
                                                     label=workflow_inputs[inp].label,
                                                     param_type=workflow_inputs[inp].type,
                                                     param_format=workflow_inputs[inp].format)
            cwl_inp.default = workflow_inputs[inp].default
            # Bypassing cwlgen type check in case of arrays
            if type(workflow_inputs[inp].type) == dict:
                cwl_inp.type = workflow_inputs[inp].type
            wf.inputs.append(cwl_inp)


        # By default only keep the output of the last stage as output
        last_stage = stages[-1]
        for o in last_stage.outputs:
            cwl_out = cwlgen.workflow.WorkflowOutputParameter(o[0], known_outputs[o[0]]+'/'+o[0],
                                                              label=o[0],
                                                              param_type='File',
                                                              param_format=o[1].__name__)
            wf.outputs.append(cwl_out)

        return wf
