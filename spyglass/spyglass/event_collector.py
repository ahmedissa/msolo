import copy
import logging
import re
import time

invalid_pattern = re.compile('[^-_\.A-Za-z0-9]')
# allow certain string patterns and int/long values
def is_valid_key(key):
  try:
    if type(key) in (int, long):
      return True
    else:
      return invalid_pattern.search(key) is None
  except:
    logging.error("can't validate '%s', (%s)", key, type(key))


class EventCollectorException(Exception):
  pass


class CounterMap(dict):
  def increment(self, key, increment=1, now=None):
    if not is_valid_key(key):
      logging.warning("invalid key: '%s'", key)
      return
    try:
      value, time_updated = self[key]
    except KeyError:
      value = 0
      time_updated = now
    value += increment
    if now > time_updated:
      time_updated = now
    self[key] = (value, time_updated)
      
  def merge(self, counter_map):
    for key, (value, time_updated) in counter_map.iteritems():
      self.increment(key, value, time_updated)

  def get_log_lines(self, concise=False):
    if concise:
      return ['%s: %s' % (key, value)
              for key, value in sorted(self.iteritems())]
    else:
      return ['%s:%s' % (key, value)
              for key, value in sorted(self.iteritems())]

  def prune(self, maximum_inactivity=3600):
    expiration_time = time.time() - maximum_inactivity
    for key, (value, time_updated) in self.items():
      if (time_updated < expiration_time or
          not is_valid_key(key)):
        del self[key]


# this is a map of countermaps
class ExecTimeMap(dict):
  # granularity - time granularity in milliseconds
  def __init__(self, granularity=10):
    self.granularity = granularity

  # exec_time - time in seconds
  def log_exec_time(self, key, exec_time, now=None):
    if not is_valid_key(key):
      logging.warning("invalid key: '%s'", key)
      return
    # normalize to granularity
    exec_time_ms = int(exec_time * 1000 / self.granularity) * self.granularity
    try:
      cm, time_updated = self[key]
    except KeyError:
      cm = CounterMap()
      time_updated = now

    cm.increment(exec_time_ms)
    if now > time_updated:
      time_updated = now
    self[key] = (cm, time_updated)

  def merge(self, exec_time_map):
    for key, counter_map in exec_time_map.iteritems():
      try:
        self[key].merge(counter_map)
      except KeyError:
        self[key] = copy.copy(counter_map)

  def prune(self, maximum_inactivity=3600):
    expiration_time = time.time() - maximum_inactivity
    for key, (value, time_updated) in self.items():
      if (time_updated < expiration_time or
          not is_valid_key(key)):
        del self[key]

  # get a resonably good looking summary of the data
  def get_lines(self):
    lines = []
    for key, counter_map in sorted(self.iteritems()):
      lines.append(key)
      for time_ms, count in sorted(counter_map.iteritems()):
        lines.append("  %sms: %s" % (time_ms, count))
    return lines

  # get a resonably good looking summary of the data for logging
  def get_log_lines(self):
    lines = []
    for key, counter_map in sorted(self.iteritems()):
      for time_ms, count in sorted(counter_map.iteritems()):
        lines.append("%s.%sms: %s" % (key, time_ms, count))
    return lines

  # condense the execution data into a statistical distribution
  def get_stats_log_lines(self):
    lines = []
    stats_map = self.get_stats_map()
    for script, summary_stats in sorted(stats_map.iteritems()):
      x = '%(min).0f/%(average).0f/%(max).0f/%(std_dev).0f %(samples)u' % summary_stats
      lines.append('%s: %s' % (script, x))
    return lines

  # fixme: this is highly inefficient because you are growing large
  # array to pass into the stat function - better to do some clever math
  # @return dictionary mapping script to min/max/avg/median/std-dev
  def get_stats_map(self):
    stats_map = {}
    for script, counter_map in self.iteritems():
      response_time_list = []
      for time_ms, count in counter_map.iteritems():
        response_time_list.extend([time_ms] * count)
      stats_map[script] = compute_statistics(response_time_list)
    return stats_map


class EventCollector(object):
  def __init__(self):
    self.counter_map = CounterMap()
    self.exec_time_map = ExecTimeMap()

  def log_exec_time(self, *args, **kargs):
    self.exec_time_map.log_exec_time(*args, **kargs)

  def increment(self, *args, **kargs):
    self.counter_map.increment(*args, **kargs)

  def merge(self, collector):
    self.counter_map.merge(collector.counter_map)
    self.exec_time_map.merge(collector.exec_time_map)

  def prune(self, maximum_inactivity=3600):
    self.counter_map.prune(maximum_inactivity)
    self.exec_time_map.prune(maximum_inactivity)

  def __str__(self):
    return '\n'.join(self.get_log_lines())

  def get_log_lines(self, use_stats_analysis=False, concise=False):
    lines = self.counter_map.get_log_lines(concise=concise)
    if use_stats_analysis:
      lines += self.exec_time_map.get_stats_log_lines()
    else:
      lines += self.exec_time_map.get_log_lines()
    return lines

  def close(self):
    close_collector(self)

class EventCollectorProxy(object):
  def __getattribute__(self, name):
    return getattr(get_event_collector(), name)

__collector_stack = []
def get_event_collector():
  try:
    return __collector_stack[-1]
  except IndexError, e:
    return __add_collector()

# alias this function, as the name suggests the correct use of this function
init_event_collector = get_event_collector

def __add_collector():
  c = EventCollector()
  __collector_stack.append(c)
  return c

def __remove_collector():
  c = __collector_stack.pop()
  get_event_collector().merge(c)
  return c

# opening a new collector lets you track events for some subset of the program
# the event are automatically merged into the root collector when you
# close the collector
def open_collector():
  # ensure the base collector is there
  get_event_collector()
  return __add_collector()

def close_collector(event_collector):
  if event_collector != __collector_stack[-1]:
    raise EventCollectorException("can't close a non-leaf event collector")
  __remove_collector()
  

def compute_statistics(numeric_range, percentile_list=None):
  # percentile_list is a list of percentiles to break out [99, 95, 90, ...]
  # return {median, average, std_dev, min, max, sample, percentile_map}
  #   of a sequence. a few stats depend on sorting - others are indifferent
  sample_count = len(numeric_range)
  numeric_range.sort()
  total = sum(numeric_range)
  avg = float(total) / sample_count
  sdsq = sum([(x-avg)**2 for x in numeric_range])
  median = numeric_range[sample_count // 2]
  std_dev = (sdsq / (sample_count - 1 or 1)) ** 0.5

  percentile_map = {}
  if percentile_list:
    for percentile in percentile_list:
      sample_index = int(sample_count * (percentile / 100.0))
      percentile_map[percentile] = numeric_range[sample_index]
      
  
  return {
    'median': median,
    'average': avg,
    'std_dev': std_dev,
    'min': min(numeric_range),
    'max': max(numeric_range),
    'samples': sample_count,
    'percentile_map': percentile_map,
  }
