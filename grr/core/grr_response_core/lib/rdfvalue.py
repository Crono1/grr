#!/usr/bin/env python
"""AFF4 RDFValue implementations.

This module contains all RDFValue implementations.

NOTE: This module uses the class registry to contain all implementations of
RDFValue class, regardless of where they are defined. To do this reliably, these
implementations must be imported _before_ the relevant classes are referenced
from this module.
"""
from __future__ import division

import abc
import calendar
import collections
import datetime
import functools
import logging
import posixpath
import re
import time
import zlib


from builtins import filter  # pylint: disable=redefined-builtin
import dateutil
from dateutil import parser
from future.utils import iteritems
from future.utils import with_metaclass
from past.builtins import long
from typing import cast

from grr_response_core.lib import registry
from grr_response_core.lib import utils

# Factor to convert from seconds to microseconds
MICROSECONDS = 1000000

# Somewhere to keep all the late binding placeholders.
_LATE_BINDING_STORE = {}


def RegisterLateBindingCallback(target_name, callback, **kwargs):
  """Registers a callback to be invoked when the RDFValue named is declared."""
  _LATE_BINDING_STORE.setdefault(target_name, []).append((callback, kwargs))


class Error(Exception):
  """Errors generated by RDFValue parsers."""


class InitializeError(Error):
  """Raised when we can not initialize from this parameter."""


class DecodeError(InitializeError, ValueError):
  """Generated when we can not decode the data."""

  def __init__(self, msg):
    logging.debug(msg)
    super(DecodeError, self).__init__(msg)


class RDFValueMetaclass(registry.MetaclassRegistry):
  """A metaclass for managing semantic values."""

  def __init__(cls, name, bases, env_dict):  # pylint: disable=no-self-argument
    super(RDFValueMetaclass, cls).__init__(name, bases, env_dict)

    # Run and clear any late binding callbacks registered for this class.
    for callback, kwargs in _LATE_BINDING_STORE.pop(name, []):
      callback(target=cls, **kwargs)


# TODO(user):pytype RDFValueMetaclass inherits MetaclassRegistry that
# inherits abc.ABCMeta, but type checker can't infer this, apparently because
# with_metaclass is used.
# pytype: disable=ignored-abstractmethod
class RDFValue(with_metaclass(RDFValueMetaclass, object)):
  """Baseclass for values.

  RDFValues are serialized to and from the data store.
  """

  # This is how the attribute will be serialized to the data store. It must
  # indicate both the type emitted by SerializeToDataStore() and expected by
  # FromDatastoreValue()
  data_store_type = "bytes"

  # URL pointing to a help page about this value type.
  context_help_url = None

  _value = None
  _age = 0

  # Mark as dirty each time we modify this object.
  dirty = False

  # If this value was created as part of an AFF4 attribute, the attribute is
  # assigned here.
  attribute_instance = None

  def __init__(self, initializer=None, age=None):
    """Constructor must be able to take no args.

    Args:
      initializer: Optional parameter to construct from.
      age: The age of this entry as an RDFDatetime. If not provided, create a
           new instance.

    Raises:
      InitializeError: if we can not be initialized from this parameter.
    """
    # Default timestamp is now.
    if age is None:
      age = RDFDatetime(age=0)

    self._age = age

    # Allow an RDFValue to be initialized from an identical RDFValue.
    # TODO(user):pytype: type checker can't infer that the initializer
    # is not None after the check below.
    if initializer.__class__ == self.__class__:
      self.ParseFromString(
          cast(self.__class__, initializer).SerializeToString())

  def Copy(self):
    """Make a new copy of this RDFValue."""
    res = self.__class__()
    res.ParseFromString(self.SerializeToString())
    return res

  def SetRaw(self, value, age=None):
    self._value = value
    if age is not None:
      self._age = age

  def __copy__(self):
    return self.Copy()

  @property
  def age(self):
    if self._age.__class__ is not RDFDatetime:
      self._age = RDFDatetime(self._age, age=0)

    return self._age

  @age.setter
  def age(self, value):
    """When assigning to this attribute it must be an RDFDatetime."""
    self._age = RDFDatetime(value, age=0)

  @abc.abstractmethod
  def ParseFromString(self, string):
    """Given a string, parse ourselves from it."""
    pass

  @abc.abstractmethod
  def ParseFromDatastore(self, value):
    """Initialize the RDF object from the datastore value."""
    pass

  @classmethod
  def FromDatastoreValue(cls, value, age=None):
    res = cls()
    res.ParseFromDatastore(value)
    if age:
      res.age = age
    return res

  @classmethod
  def FromSerializedString(cls, value, age=None):
    res = cls()
    res.ParseFromString(value)
    if age:
      res.age = age
    return res

  def SerializeToDataStore(self):
    """Serialize to a datastore compatible form."""
    return self.SerializeToString()

  @abc.abstractmethod
  def SerializeToString(self):
    """Serialize into a string which can be parsed using ParseFromString."""

  @classmethod
  def Fields(cls):
    """Return a list of fields which can be queried from this value."""
    return []

  def __eq__(self, other):
    return self._value == other

  def __ne__(self, other):
    return not self.__eq__(other)

  def __hash__(self):
    return hash(self.SerializeToString())

  def __bool__(self):
    return bool(self._value)

  def __nonzero__(self):
    return bool(self._value)

  def __str__(self):  # pylint: disable=super-on-old-class
    """Ignores the __repr__ override below to avoid indefinite recursion."""
    return super(RDFValue, self).__repr__()

  def __repr__(self):
    content = utils.SmartStr(self)
    if len(content) > 100:
      content = content[:100] + "..."

    # Note %r, which prevents nasty nonascii characters from being printed,
    # including dangerous terminal escape sequences.
    return "<%s(%r)>" % (self.__class__.__name__, content)


class RDFPrimitive(RDFValue):

  @classmethod
  def FromHumanReadable(cls, string):
    instance = cls()
    instance.ParseFromHumanReadable(string)
    return instance

  @abc.abstractmethod
  def ParseFromHumanReadable(self, string):
    """Initializes the object from human-readable string.

    Args:
      string: An `unicode` value to initialize the object from.
    """


# pytype: enable=ignored-abstractmethod


class RDFBytes(RDFPrimitive):
  """An attribute which holds bytes."""
  data_store_type = "bytes"

  _value = ""

  def __init__(self, initializer=None, age=None):
    super(RDFBytes, self).__init__(initializer=initializer, age=age)
    if not self._value and initializer is not None:
      self.ParseFromString(initializer)

  def ParseFromString(self, string):
    utils.AssertType(string, bytes)
    self._value = string

  def ParseFromDatastore(self, value):
    utils.AssertType(value, bytes)
    self._value = value

  def ParseFromHumanReadable(self, string):
    utils.AssertType(string, unicode)
    self._value = string.encode("utf-8")

  def AsBytes(self):
    return self._value

  def SerializeToString(self):
    return self._value

  def __str__(self):
    return utils.SmartStr(self._value)

  def __lt__(self, other):
    if isinstance(other, self.__class__):
      return self._value < other._value  # pylint: disable=protected-access
    else:
      return self._value < other

  def __gt__(self, other):
    if isinstance(other, self.__class__):
      return self._value > other._value  # pylint: disable=protected-access
    else:
      return self._value > other

  def __eq__(self, other):
    if isinstance(other, self.__class__):
      return self._value == other._value  # pylint: disable=protected-access
    else:
      return self._value == other

  def __len__(self):
    return len(self._value)


class RDFZippedBytes(RDFBytes):
  """Zipped bytes sequence."""

  def Uncompress(self):
    if self:
      return zlib.decompress(self._value)
    else:
      return ""


@functools.total_ordering
class RDFString(RDFPrimitive):
  """Represent a simple string."""

  data_store_type = "string"

  _value = u""

  # TODO(hanuszczak): Allow initializng form arbitrary `unicode`-able object.
  def __init__(self, initializer=None, age=None):
    super(RDFString, self).__init__(initializer=None, age=age)

    if isinstance(initializer, RDFString):
      self._value = initializer._value  # pylint: disable=protected-access
    elif isinstance(initializer, bytes):
      self.ParseFromString(initializer)
    elif isinstance(initializer, unicode):
      self._value = initializer
    elif initializer is not None:
      message = "Unexpected initializer `%s` of type `%s`"
      message %= (initializer, type(initializer))
      raise TypeError(message)

  def format(self, *args, **kwargs):  # pylint: disable=invalid-name
    return self._value.format(*args, **kwargs)

  def split(self, *args, **kwargs):  # pylint: disable=invalid-name
    return self._value.split(*args, **kwargs)

  def __str__(self):
    return self._value.encode("utf-8")

  def __unicode__(self):
    return self._value

  def __getitem__(self, item):
    return self._value.__getitem__(item)

  def __eq__(self, other):
    if isinstance(other, RDFString):
      return self._value == other._value  # pylint: disable=protected-access

    if isinstance(other, unicode):
      return self._value == other

    # TODO(hanuszczak): Comparing `RDFString` and `bytes` should result in type
    # error. For now we allow it because too many tests still use non-unicode
    # string literals.
    if isinstance(other, bytes):
      return self._value.encode("utf-8") == other

    message = "Unexpected value `%s` of type `%s`"
    message %= (other, type(other))
    raise TypeError(message)

  def __lt__(self, other):
    if isinstance(other, RDFString):
      return self._value < other._value  # pylint: disable=protected-access

    if isinstance(other, unicode):
      return self._value < other

    # TODO(hanuszczak): Comparing `RDFString` and `bytes` should result in type
    # error. For now we allow it because too many tests still use non-unicode
    # string literals.
    if isinstance(other, bytes):
      return self._value.encode("utf-8") < other

    message = "Unexpected value `%s` of type `%s`"
    message %= (other, type(other))
    raise TypeError(message)

  def ParseFromString(self, string):
    utils.AssertType(string, bytes)
    self._value = string.decode("utf-8")

  def ParseFromDatastore(self, value):
    utils.AssertType(value, unicode)
    self._value = value

  def ParseFromHumanReadable(self, string):
    utils.AssertType(string, unicode)
    self._value = string

  def SerializeToString(self):
    return self._value.encode("utf-8")

  def SerializeToDataStore(self):
    return self._value


# TODO(hanuszczak): This class should provide custom method for parsing from
# human readable strings (and arguably should not derive from `RDFBytes` at
# all).
class HashDigest(RDFBytes):
  """Binary hash digest with hex string representation."""

  data_store_type = "bytes"

  def HexDigest(self):
    return self._value.encode("hex")

  def __str__(self):
    return self._value.encode("hex")

  def __eq__(self, other):
    return (self._value == utils.SmartStr(other) or
            self._value.encode("hex") == other)

  def __ne__(self, other):
    return not self.__eq__(other)


@functools.total_ordering
class RDFInteger(RDFPrimitive):
  """Represent an integer."""

  data_store_type = "integer"

  @staticmethod
  def IsNumeric(value):
    return isinstance(value, (int, long, float, RDFInteger))

  def __init__(self, initializer=None, age=None):
    super(RDFInteger, self).__init__(initializer=initializer, age=age)
    if self._value is None:
      if initializer is None:
        self._value = 0
      else:
        self._value = int(initializer)

  def SerializeToString(self):
    return str(self._value)

  def ParseFromString(self, string):
    self._value = 0
    if string:
      try:
        self._value = int(string)
      except TypeError as e:
        raise DecodeError(e)

  def ParseFromDatastore(self, value):
    utils.AssertType(value, int)
    self._value = value

  def ParseFromHumanReadable(self, string):
    utils.AssertType(string, unicode)
    self._value = int(string)

  def __str__(self):
    return str(self._value)

  def __unicode__(self):
    return unicode(self._value)

  @classmethod
  def FromDatastoreValue(cls, value, age=None):
    return cls(initializer=value, age=age)

  def SerializeToDataStore(self):
    """Use varint to store the integer."""
    return self._value

  def __long__(self):
    return int(self._value)

  def __int__(self):
    return int(self._value)

  def __float__(self):
    return float(self._value)

  def __index__(self):
    return self._value

  def __lt__(self, other):
    return self._value < other

  def __and__(self, other):
    return self._value & other

  def __rand__(self, other):
    return self._value & other

  def __iand__(self, other):
    self._value &= other
    return self

  def __or__(self, other):
    return self._value | other

  def __ror__(self, other):
    return self._value | other

  def __ior__(self, other):
    self._value |= other
    return self

  def __add__(self, other):
    return self._value + other

  def __radd__(self, other):
    return self._value + other

  def __iadd__(self, other):
    self._value += other
    return self

  def __sub__(self, other):
    return self._value - other

  def __rsub__(self, other):
    return other - self._value

  def __isub__(self, other):
    self._value -= other
    return self

  def __mul__(self, other):
    return self._value * other

  # TODO(hanuszczak): There are no `__rop__` methods in Python 3 so all of these
  # should be removed. Also, in general it should not be possible to add two
  # values with incompatible types (e.g. `RDFInteger` and `int`). Sadly,
  # currently a lot of code depends on this behaviour but it should be changed
  # in the future.
  def __rmul__(self, other):
    return self._value * other

  def __div__(self, other):
    return self._value.__div__(other)

  def __truediv__(self, other):
    return self._value.__truediv__(other)

  def __floordiv__(self, other):
    return self._value.__floordiv__(other)

  def __hash__(self):
    return hash(self._value)


class RDFBool(RDFInteger):
  """Boolean value."""
  data_store_type = "unsigned_integer"

  def ParseFromHumanReadable(self, string):
    utils.AssertType(string, unicode)

    upper_string = string.upper()
    if upper_string == u"TRUE" or string == u"1":
      self._value = 1
    elif upper_string == u"FALSE" or string == u"0":
      self._value = 0
    else:
      raise ValueError("Unparsable boolean string: `%s`" % string)


class RDFDatetime(RDFInteger):
  """A date and time internally stored in MICROSECONDS."""
  converter = MICROSECONDS
  data_store_type = "unsigned_integer"

  def __init__(self, initializer=None, age=None):
    super(RDFDatetime, self).__init__(None, age)

    self._value = 0

    if initializer is None:
      return

    if isinstance(initializer, (RDFInteger, int, long, float)):
      self._value = int(initializer)
    else:
      raise InitializeError(
          "Unknown initializer for RDFDateTime: %s." % type(initializer))

  @classmethod
  def Now(cls):
    return cls(int(time.time() * cls.converter))

  def Format(self, fmt):
    """Return the value as a string formatted as per strftime semantics."""
    return time.strftime(fmt, time.gmtime(self._value / self.converter))

  def __str__(self):
    """Return the date in human readable (UTC)."""
    return self.Format("%Y-%m-%d %H:%M:%S")

  def __unicode__(self):
    return utils.SmartUnicode(str(self))

  def AsDatetime(self):
    """Return the time as a python datetime object."""
    return datetime.datetime.utcfromtimestamp(self._value / self.converter)

  def AsSecondsSinceEpoch(self):
    return self._value // self.converter

  def AsMicrosecondsSinceEpoch(self):
    return self._value

  @classmethod
  def FromSecondsSinceEpoch(cls, value):
    # Convert to int in case we get fractional seconds with higher
    # resolution than what this class supports.
    return cls(int(value * cls.converter))

  @classmethod
  def FromDatetime(cls, value):
    res = cls()
    seconds = calendar.timegm(value.utctimetuple())
    res.SetRaw((seconds * cls.converter) + value.microsecond)
    return res

  @classmethod
  def FromHumanReadable(cls, value, eoy=False):
    res = cls()
    res.ParseFromHumanReadable(value, eoy=eoy)
    return res

  @classmethod
  def Lerp(cls, t, start_time, end_time):
    """Interpolates linearly between two datetime values.

    Args:
      t: An interpolation "progress" value.
      start_time: A value for t = 0.
      end_time: A value for t = 1.

    Returns:
      An interpolated `RDFDatetime` instance.

    Raises:
      TypeError: If given time values are not instances of `RDFDatetime`.
      ValueError: If `t` parameter is not between 0 and 1.
    """
    if not (isinstance(start_time, RDFDatetime) and
            isinstance(end_time, RDFDatetime)):
      raise TypeError("Interpolation of non-datetime values")

    if not 0.0 <= t <= 1.0:
      raise ValueError("Interpolation progress does not belong to [0.0, 1.0]")

    return RDFDatetime(round((1 - t) * start_time._value + t * end_time._value))  # pylint: disable=protected-access

  def ParseFromHumanReadable(self, string, eoy=False):
    # TODO(hanuszczak): This method should accept only unicode literals.
    self._value = self._ParseFromHumanReadable(string, eoy=eoy)

  def __add__(self, other):
    if isinstance(other, (int, long, float, Duration)):
      # Assume other is in seconds
      return self.__class__(self._value + other * self.converter)

    return NotImplemented

  def __iadd__(self, other):
    if isinstance(other, (int, long, float, Duration)):
      # Assume other is in seconds
      self._value += other * self.converter
      return self

    return NotImplemented

  def __mul__(self, other):
    if isinstance(other, (int, long, float, Duration)):
      return self.__class__(self._value * other)

    return NotImplemented

  def __rmul__(self, other):
    return self.__mul__(other)

  def __sub__(self, other):
    if isinstance(other, (int, long, float, Duration)):
      # Assume other is in seconds
      return self.__class__(self._value - other * self.converter)

    if isinstance(other, RDFDatetime):
      return Duration(self.AsSecondsSinceEpoch() - other.AsSecondsSinceEpoch())

    return NotImplemented

  def __isub__(self, other):
    if isinstance(other, (int, long, float, Duration)):
      # Assume other is in seconds
      self._value -= other * self.converter
      return self

    return NotImplemented

  @classmethod
  def _ParseFromHumanReadable(cls, string, eoy=False):
    """Parse a human readable string of a timestamp (in local time).

    Args:
      string: The string to parse.
      eoy: If True, sets the default value to the end of the year.
           Usually this method returns a timestamp where each field that is
           not present in the given string is filled with values from the date
           January 1st of the current year, midnight. Sometimes it makes more
           sense to compare against the end of a period so if eoy is set, the
           default values are copied from the 31st of December of the current
           year, 23:59h.

    Returns:
      The parsed timestamp.
    """
    # TODO(hanuszczak): Date can come either as a single integer (which we
    # interpret as a timestamp) or as a really human readable thing such as
    # '2000-01-01 13:37'. This is less than ideal (since timestamps are not
    # really "human readable) and should be fixed in the future.
    try:
      return int(string)
    except ValueError:
      pass

    # By default assume the time is given in UTC.
    # pylint: disable=g-tzinfo-datetime
    if eoy:
      default = datetime.datetime(
          time.gmtime().tm_year, 12, 31, 23, 59, tzinfo=dateutil.tz.tzutc())
    else:
      default = datetime.datetime(
          time.gmtime().tm_year, 1, 1, 0, 0, tzinfo=dateutil.tz.tzutc())
    # pylint: enable=g-tzinfo-datetime

    timestamp = parser.parse(string, default=default)

    return calendar.timegm(timestamp.utctimetuple()) * cls.converter

  def Floor(self, interval):
    if not isinstance(interval, Duration):
      raise TypeError("Expected `Duration`, got `%s`" % interval.__class__)

    seconds = self.AsSecondsSinceEpoch() // interval.seconds * interval.seconds
    return self.FromSecondsSinceEpoch(seconds)


class RDFDatetimeSeconds(RDFDatetime):
  """A DateTime class which is stored in whole seconds."""
  converter = 1


class Duration(RDFInteger):
  """Duration value stored in seconds internally."""
  data_store_type = "unsigned_integer"

  # pyformat: disable
  DIVIDERS = collections.OrderedDict((
      ("w", 60 * 60 * 24 * 7),
      ("d", 60 * 60 * 24),
      ("h", 60 * 60),
      ("m", 60),
      ("s", 1)))
  # pyformat: enable

  def __init__(self, initializer=None, age=None):
    super(Duration, self).__init__(None, age)
    if isinstance(initializer, Duration):
      self._value = initializer._value  # pylint: disable=protected-access
    elif isinstance(initializer, basestring):
      self.ParseFromHumanReadable(initializer)
    elif isinstance(initializer, (int, long, float)):
      self._value = initializer
    elif isinstance(initializer, RDFInteger):
      self._value = int(initializer)
    elif initializer is None:
      self._value = 0
    else:
      raise InitializeError(
          "Unknown initializer for Duration: %s." % type(initializer))

  @classmethod
  def FromSeconds(cls, seconds):
    return cls(seconds)

  def Validate(self, value, **_):
    self.ParseFromString(value)

  def ParseFromString(self, string):
    self.ParseFromHumanReadable(string)

  def SerializeToString(self):
    return str(self)

  @property
  def seconds(self):
    return self._value

  @property
  def microseconds(self):
    return self._value * 1000000

  def __str__(self):
    time_secs = self._value
    for label, divider in iteritems(self.DIVIDERS):
      if time_secs % divider == 0:
        return "%d%s" % (time_secs // divider, label)

  def __unicode__(self):
    return utils.SmartUnicode(str(self))

  def __add__(self, other):
    if isinstance(other, (int, long, float, Duration)):
      # Assume other is in seconds
      return self.__class__(self._value + other)

    return NotImplemented

  def __iadd__(self, other):
    if isinstance(other, (int, long, float, Duration)):
      # Assume other is in seconds
      self._value += other
      return self

    return NotImplemented

  def __mul__(self, other):
    if isinstance(other, (int, long, float, Duration)):
      return self.__class__(int(self._value * other))

    return NotImplemented

  def __rmul__(self, other):
    return self.__mul__(other)

  def __sub__(self, other):
    if isinstance(other, (int, long, float, Duration)):
      # Assume other is in seconds
      return self.__class__(self._value - other)

    return NotImplemented

  def __isub__(self, other):
    if isinstance(other, (int, long, float, Duration)):
      # Assume other is in seconds
      self._value -= other
      return self

    return NotImplemented

  def __abs__(self):
    return Duration(abs(self._value))

  def Expiry(self, base_time=None):
    if base_time is None:
      base_time = RDFDatetime.Now()
    else:
      base_time = base_time.Copy()

    base_time_sec = base_time.AsSecondsSinceEpoch()

    return RDFDatetime.FromSecondsSinceEpoch(base_time_sec + self._value)

  def ParseFromHumanReadable(self, timestring):
    """Parse a human readable string of a duration.

    Args:
      timestring: The string to parse.
    """
    if not timestring:
      return

    orig_string = timestring

    multiplicator = 1

    if timestring[-1].isdigit():
      pass
    else:
      try:
        multiplicator = self.DIVIDERS[timestring[-1]]
      except KeyError:
        raise RuntimeError("Invalid duration multiplicator: '%s' ('%s')." %
                           (timestring[-1], orig_string))

      timestring = timestring[:-1]

    try:
      self._value = int(timestring) * multiplicator
    except ValueError:
      raise InitializeError(
          "Could not parse expiration time '%s'." % orig_string)


class ByteSize(RDFInteger):
  """A size for bytes allowing standard unit prefixes.

  We use the standard IEC 60027-2 A.2 and ISO/IEC 80000:
  Binary units (powers of 2): Ki, Mi, Gi
  SI units (powers of 10): k, m, g
  """
  data_store_type = "unsigned_integer"

  DIVIDERS = dict((
      ("", 1),
      ("k", 1000),
      ("m", 1000**2),
      ("g", 1000**3),
      ("ki", 1024),
      ("mi", 1024**2),
      ("gi", 1024**3),
  ))

  REGEX = re.compile("^([0-9.]+)([kmgi]*)b?$", re.I)

  def __init__(self, initializer=None, age=None):
    super(ByteSize, self).__init__(None, age)
    if isinstance(initializer, ByteSize):
      self._value = initializer._value  # pylint: disable=protected-access
    elif isinstance(initializer, basestring):
      self.ParseFromHumanReadable(initializer)
    elif isinstance(initializer, (int, long, float)):
      self._value = initializer
    elif isinstance(initializer, RDFInteger):
      self._value = int(initializer)
    elif initializer is None:
      self._value = 0
    else:
      raise InitializeError(
          "Unknown initializer for ByteSize: %s." % type(initializer))

  def __str__(self):
    size_token = ""
    if self._value > 1024**3:
      size_token = "GiB"
      value = self._value / 1024**3
    elif self._value > 1024**2:
      size_token = "MiB"
      value = self._value / 1024**2
    elif self._value > 1024:
      size_token = "KiB"
      value = self._value / 1024
    else:
      return utils.SmartStr(self._value) + "B"

    return "%.1f%s" % (value, size_token)

  def ParseFromHumanReadable(self, string):
    """Parse a human readable string of a byte string.

    Args:
      string: The string to parse.

    Raises:
      DecodeError: If the string can not be parsed.
    """
    if not string:
      return None

    match = self.REGEX.match(string.strip().lower())
    if not match:
      raise DecodeError("Unknown specification for ByteSize %s" % string)

    multiplier = self.DIVIDERS.get(match.group(2))
    if not multiplier:
      raise DecodeError("Invalid multiplier %s" % match.group(2))

    # The value may be represented as a float, but if not dont lose accuracy.
    value = match.group(1)
    if "." in value:
      value = float(value)
    else:
      value = int(value)

    self._value = int(value * multiplier)


@functools.total_ordering
class RDFURN(RDFValue):
  """An object to abstract URL manipulation."""

  data_store_type = "string"

  # Careful when changing this value, this is hardcoded a few times in this
  # class for performance reasons.
  scheme = "aff4"

  _string_urn = ""

  def __init__(self, initializer=None, age=None):
    """Constructor.

    Args:
      initializer: A string or another RDFURN.
      age: The age of this entry.
    """
    # This is a shortcut that is a bit faster than the standard way of
    # using the RDFValue constructor to make a copy of the class. For
    # RDFURNs that way is a bit slow since it would try to normalize
    # the path again which is not needed - it comes from another
    # RDFURN so it is already in the correct format.
    if isinstance(initializer, RDFURN):
      # Make a direct copy of the other object
      self._string_urn = initializer.Path()
      super(RDFURN, self).__init__(None, age=age)
      return

    super(RDFURN, self).__init__(initializer=initializer, age=age)
    if self._value is None and initializer is not None:
      self.ParseFromString(initializer)

  def ParseFromString(self, initializer):
    """Create RDFRUN from string.

    Args:
      initializer: url string
    """
    # Strip off the aff4: prefix if necessary.
    if initializer.startswith("aff4:/"):
      initializer = initializer[5:]

    self._string_urn = utils.NormalizePath(initializer)

  def ParseFromDatastore(self, value):
    utils.AssertType(value, unicode)
    # TODO(hanuszczak): We should just assign the `self._string_urn` here
    # instead of including all of the parsing magic since the data store values
    # should be normalized already. But sadly this is not the case and for now
    # we have to deal with unnormalized values as well.
    self.ParseFromString(value)

  def SerializeToString(self):
    return str(self)

  def SerializeToDataStore(self):
    return unicode(self)

  def Dirname(self):
    return posixpath.dirname(self._string_urn)

  def Basename(self):
    return posixpath.basename(self.Path())

  def Add(self, path, age=None):
    """Add a relative stem to the current value and return a new RDFURN.

    If urn is a fully qualified URN, replace the current value with it.

    Args:
      path: A string containing a relative path.
      age: The age of the object. If None set to current time.

    Returns:
       A new RDFURN that can be chained.

    Raises:
       ValueError: if the path component is not a string.
    """
    if not isinstance(path, basestring):
      raise ValueError("Only strings should be added to a URN.")

    result = self.Copy(age)
    result.Update(path=utils.JoinPath(self._string_urn, path))

    return result

  def Update(self, url=None, path=None):
    """Update one of the fields.

    Args:
       url: An optional string containing a URL.
       path: If the path for this URN should be updated.
    """
    if url:
      self.ParseFromString(url)
    if path:
      self._string_urn = path
    self.dirty = True

  def Copy(self, age=None):
    """Make a copy of ourselves."""
    if age is None:
      age = int(time.time() * MICROSECONDS)
    return self.__class__(self, age=age)

  def __str__(self):
    return utils.SmartStr("aff4:%s" % self._string_urn)

  def __unicode__(self):
    return utils.SmartUnicode(u"aff4:%s" % self._string_urn)

  def __eq__(self, other):
    if isinstance(other, basestring):
      other = self.__class__(other)

    elif other is None:
      return False

    elif not isinstance(other, RDFURN):
      return NotImplemented

    return self._string_urn == other.Path()

  def __bool__(self):
    return bool(self._string_urn)

  def __nonzero__(self):
    return bool(self._string_urn)

  def __lt__(self, other):
    return self._string_urn < other

  def Path(self):
    """Return the path of the urn."""
    return self._string_urn

  def Split(self, count=None):
    """Returns all the path components.

    Args:
      count: If count is specified, the output will be exactly this many path
        components, possibly extended with the empty string. This is useful for
        tuple assignments without worrying about ValueErrors:

           namespace, path = urn.Split(2)

    Returns:
      A list of path components of this URN.
    """
    if count:
      result = list(filter(None, self._string_urn.split("/", count)))
      while len(result) < count:
        result.append("")

      return result

    else:
      return list(filter(None, self._string_urn.split("/")))

  def RelativeName(self, volume):
    """Given a volume URN return the relative URN as a unicode string.

    We remove the volume prefix from our own.
    Args:
      volume: An RDFURN or fully qualified url string.

    Returns:
      A string of the url relative from the volume or None if our URN does not
      start with the volume prefix.
    """
    string_url = utils.SmartUnicode(self)
    volume_url = utils.SmartUnicode(volume)
    if string_url.startswith(volume_url):
      result = string_url[len(volume_url):]
      # This must always return a relative path so we strip leading "/"s. The
      # result is always a unicode string.
      return result.lstrip("/")

    return None

  def __repr__(self):
    return "<%s age=%s>" % (self, self.age)


class Subject(RDFURN):
  """A psuedo attribute representing the subject of an AFF4 object."""


DEFAULT_FLOW_QUEUE = RDFURN("F")


class SessionID(RDFURN):
  """An rdfvalue object that represents a session_id."""

  def __init__(self,
               initializer=None,
               age=None,
               base="aff4:/flows",
               queue=DEFAULT_FLOW_QUEUE,
               flow_name=None):
    """Constructor.

    Args:
      initializer: A string or another RDFURN.
      age: The age of this entry.
      base: The base namespace this session id lives in.
      queue: The queue to use.
      flow_name: The name of this flow or its random id.
    Raises:
      InitializeError: The given URN cannot be converted to a SessionID.
    """
    if initializer is None:
      # This SessionID is being constructed from scratch.
      if flow_name is None:
        flow_name = utils.PRNG.GetUInt32()

      if isinstance(flow_name, int):
        initializer = RDFURN(base).Add("%s:%X" % (queue.Basename(), flow_name))
      else:
        initializer = RDFURN(base).Add("%s:%s" % (queue.Basename(), flow_name))
    else:
      # TODO(user): Uncomment and make all the tests pass.
      # if isinstance(initializer, (basestring, RDFString)):
      #   initializer = RDFURN(initializer)

      if isinstance(initializer, RDFURN):
        try:
          self.ValidateID(initializer.Basename())
        except ValueError as e:
          raise InitializeError(
              "Invalid URN for SessionID: %s, %s" % (initializer, e.message))

    super(SessionID, self).__init__(initializer=initializer, age=age)

  def Queue(self):
    return RDFURN(self.Basename().split(":")[0])

  def FlowName(self):
    return self.Basename().split(":", 1)[1]

  def Add(self, path, age=None):
    # Adding to a SessionID results in a normal RDFURN.
    return RDFURN(self).Add(path, age=age)

  @classmethod
  def ValidateID(cls, id_str):
    # This check is weaker than it could be because we allow queues called
    # "DEBUG-user1" and IDs like "TransferStore". We also have to allow
    # flows session ids like H:123456:hunt.
    allowed_re = re.compile(r"^[-0-9a-zA-Z]+:[0-9a-zA-Z]+(:[0-9a-zA-Z]+)?$")
    if not allowed_re.match(id_str):
      raise ValueError("Invalid SessionID: %s" % id_str)


class FlowSessionID(SessionID):

  # TODO(amoser): This is code to fix some legacy issues. Remove this when all
  # clients are built after Dec 2014.

  def ParseFromString(self, initializer):
    # Old clients sometimes send bare well known flow ids.
    if not utils.SmartStr(initializer).startswith("aff4"):
      initializer = "aff4:/flows/" + initializer
    super(FlowSessionID, self).ParseFromString(initializer)
