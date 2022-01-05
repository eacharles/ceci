""" A small module with functionality to handle configuration in a way that works
for both interactive and ballistic applications """


def cast_value(dtype, value): #pylint: disable=too-many-return-statements
    """Casts an input value to a particular type

    Parameters
    ----------
    dtype : type
        The type we are casting to
    value : ...
        The value being cast

    Returns
    -------
    out : ...
        The object cast to dtype

    Raises
    ------
    TypeError if neither value nor dtype are None and the casting fails

    Notes
    -----
    This will proceed in the following order
        1.  If dtype is None it will simply return value
        2.  If value is None it will return None
        3.  If value is an instance of dtype it will return value
        4.  If value is a Mapping it will use it as a keyword dictionary to the constructor of dtype, i.e., return dtype(**value)
        5.  It will try to pass value to the constructor of dtype, i.e., return dtype(value)
        6.  If all of these fail, it will raise a TypeError
    """
    # dtype is None means all values are legal
    if dtype is None:
        return value
    # value is None is always allowed
    if value is None:
        return None
    # if value is an instance of self.dtype, then return it
    if isinstance(value, dtype):
        return value
    # try the constructor of dtype
    try:
        return dtype(value)
    except (TypeError, ValueError):
        pass

    msg = f"Value of type {type(value)}, when {str(dtype)} was expected."
    raise TypeError(msg)


def cast_to_streamable(value):
    """Cast a value to something that yaml can easily stream"""
    if isinstance(value, dict):
        return value
    if isinstance(value, StageParameter):
        return value.value
    return value


class StageParameter:
    """ A small class to manage a single parameter with basic type checking
    """

    def __init__(self, **kwargs):
        """ Build from keywords

        Keywords
        --------
        dtype : `type` or `None`
            The data type for this parameter
        default : `dtype` or `None`
            The default value
        format : `str`
            A formatting string for printout and representation
        help : `str`
            A help or docstring
        """
        kwcopy = kwargs.copy()
        self._help = kwcopy.pop('help', 'A Parameter')
        self._format = kwcopy.pop('format', '%s')
        self._dtype = kwcopy.pop('dtype', None)
        self._default = kwcopy.pop('default', None)
        if kwcopy:  # pragma: no cover
            raise ValueError(f"Unknown arguments to StageParameter {str(list(kwcopy.keys()))}")
        self._value = cast_value(self._dtype, self._default)

    @property
    def value(self):
        """ Return the value """
        return self._value

    @property
    def dtype(self):
        """ Return the data type """
        return self._dtype

    @property
    def default(self):
        """ Return the default value """
        return self._default

    def set(self, value):
        """ Set the value, raising a TypeError if the value is the wrong type """
        self._value = cast_value(self._dtype, value)
        return self._value

    def set_to_default(self):
        """ Set the value to the default """
        self._value = cast_value(self._dtype, self._default)
        return self._value


class StageConfig(dict):
    """ A small class to manage a dictionary of configuration parameters with basic type checking
    """

    def __init__(self, **kwargs):
        """ Build from keywords

        Note
        ----
        The keywords are used as keys for the configuration parameters

        The values are used to define the allowed data type and default values

        For each key-value pair:
        If the value is a type then it will define the data type and the default will be `None`
        If the value is a value then it will set the default value define the data type as type(value)
        """
        dict.__init__(self)
        for key, val in kwargs.items():
            if val is None:
                dtype = None
                default = None
            elif isinstance(val, type):
                dtype = val
                default = None
            else:
                dtype = type(val)
                default = val
            param = StageParameter(dtype=dtype, default=default)
            self[key] = param

    def __str__(self):
        """ Override __str__ casting to deal with `StageParameter` object in the map """
        s = "{"
        for key, attr in self.items():
            if isinstance(attr, StageParameter):
                val = attr.value
            else:
                val = attr
            s += f"{key}:{val},"
        s += "}"
        return s

    def __repr__(self):
        """ A custom representation """
        s = "StageConfig"
        s += self.__str__()
        return s

    def __getitem__(self, key):
        """ Override the __getitem__ to work with `StageParameter` """
        attr = dict.__getitem__(self, key)
        if isinstance(attr, StageParameter):
            return attr.value
        return attr

    def __setitem__(self, key, value):
        """ Override the __setitem__ to work with `StageParameter` """
        if key in self:
            attr = dict.__getitem__(self, key)
            if isinstance(attr, StageParameter):
                return attr.set(value)
        dict.__setitem__(self, key, value)
        return value

    def __getattr__(self, key):
        """ Allow attribute-like parameter access """
        return self.__getitem__(key)

    def __setattr__(self, key, value):
        """ Allow attribute-like parameter setting """
        return self.__setitem__(key, value)

    def set_config(self, input_config, args):
        """ Utility function to load configuration

        Parameters
        ----------
        input_config : `dict, (str, value)`
            `dict` with key-value pairs for all the parameters
        args : `dict, (str, value)`
            `dict` with key-value pairs for all the parameters that can serve as overrides
        """
        for key in self.keys():
            val = None
            if key in input_config:
                val = input_config[key]
            if args.get(key) is not None:
                val = args[key]
            if val is None:
                attr = self.get(key)
                if attr.default is None:
                    raise ValueError(f"Missing configuration option {key}")
                val = attr.default
            self.__setattr__(key, val)

        for key, val in input_config.items():
            if key in self:
                continue
            self[key] = val

        for key, val in args.items():
            if key in self:
                continue
            self[key] = val


    def reset(self):
        """ Reset values to their defaults """
        for _, val in self.items():
            if isinstance(val, StageParameter):
                val.set_to_default()

    def get_type(self, key):
        """Get the type associated to a particular configuration parameter"""
        attr = dict.__getitem__(self, key)
        if isinstance(attr, StageParameter):
            return attr.dtype
        return type(attr)
