#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tool for merging Schedule files.

Reads a YAML-file specifying how a Eclipse Schedule section is to be
produced given certain input files.

Output will not be generated unless the produced data is valid in
Eclipse, checking provided by sunbeam/opm-parser.

YAML-file components:

 init - filename for the initial file. If omitted, defaults to an
        empty file. If you need something to happen between the
        Eclipse start date and the first DATES keyword, it must
        be present in this file.

 output - filename for output. stdout if omitted

 startdate - YYYY-MM-DD for the initial date in the simulation. This
             date is not outputted and anything occuring before that
             will be clipped (TODO)

 refdate - if supplied, will work as a reference date for relative
           inserts. If not supplied startdate will be used.

 enddate - YYYY-MM-DD, anything after that date will be clipped (TODO).

 dategrid - a string being either 'weekly', 'biweekly', 'monthly',
            'bimonthly' stating how often a DATES keyword is wanted
            (independent of inserts/merges).  '(bi)monthly' and
            'yearly' will be rounded to first in every month.

 merge - list of filenames to be merged in. DATES must be the first
         keyword in these files.

 insert - list of components to be inserted into the final Schedule
          file. Each list elemen can contain the elemens:

            date - Fixed date for the insertion

            days - relative date for insertion relative to refdate/startdate

            filename - filename to override the yaml-component element name.

            string - instead of filename, you can write the contents inline

            substitute - key-value pairs that will subsitute <key> in
                         incoming files (or inline string) with
                         associated values.

"""

import datetime
import tempfile
import argparse
import yaml
from sunbeam.tools import TimeVector

import resscript.header as header


def datetime_from_date(date):
    return datetime.datetime.combine(date, datetime.datetime.min.time())

def process_sch_config(sunschconf, quiet=True):
    """Process a Schedule configuration into a sunbeam TimeVector

    :param sunschconf : configuration for the schedule merges and inserts
    :type sunschconf: dict
    :param quiet: Whether status messages should be printed during processing
    :type quiet: bool
    """
    if 'startdate' in sunschconf:
        schedule = TimeVector(sunschconf['startdate'])
    elif 'refdate' in sunschconf:
        schedule = TimeVector(sunschconf['refdate'])
    else:
        raise Exception("No startdate or refdate given")

    if 'refdate' not in sunschconf and 'startdate' in sunschconf:
        sunschconf['refdate'] = sunschconf['startdate']

    if 'init' in sunschconf:
        if not quiet:
            print("Loading " + sunschconf['init'] + " at startdate")
        schedule.load(sunschconf['init'],
                      date=datetime.datetime.combine(sunschconf['startdate'],
                                                     datetime.datetime.min.time()))

    if 'merge' in sunschconf:
        for file in sunschconf['merge']:
            try:
                if not quiet:
                    print("Loading " + file)
                schedule.load(file)
            except ValueError as exception:
                raise Exception("Error in " + file + ": " + str(exception))

    if 'insert' in sunschconf: # inserts should be list of dicts of dicts
        for file in sunschconf['insert']:
            # file is now a dict with only one key
            fileid = file.keys()[0]
            filedata = file[fileid].keys()


            # Figure out the correct filename, only needed when we
            # have a string.
            if 'string' not in filedata:
                if 'filename' not in filedata:
                    filename = fileid
                else:
                    filename = file[fileid]['filename']

            resultfile = tempfile.NamedTemporaryFile(delete=False)
            resultfilename = resultfile.name
            if 'substitute' in filedata:
                templatelines = open(filename, 'r').readlines()

                # Parse substitution list:
                substdict = file[fileid]['substitute']
                # Perform substitution and put into a tmp file
                for line in templatelines:
                    for key in substdict:
                        if "<" + key + ">" in line:
                            line = line.replace("<" + key + ">", str(substdict[key]))
                    resultfile.write(line)
                resultfile.close()
                # Now we overwrite the filename coming from the yaml file!
                filename = resultfilename

            # Figure out the correct date:
            if 'date' in file[fileid]:
                date = datetime.datetime.combine(file[fileid]['date'],
                                                 datetime.datetime.min.time())
            if 'days' in file[fileid]:
                if not 'refdate' in sunschconf:
                    raise Exception("ERROR: When using days in insert " + \
                                    "statements, you must provide refdate")
                date = datetime.datetime.combine(sunschconf['refdate'],
                                datetime.datetime.min.time()) + \
                                datetime.timedelta(days=file[fileid]['days'])
            if 'string' not in filedata:
                schedule.load(filename, date=date)
            else:
                schedule.add_keywords(datetime_from_date(date),
                                      [file[fileid]['string']])

    if 'enddate' not in sunschconf:
        if not quiet:
            print("Warning: Implicit end date. " +\
                  "Any content at last date is ignored")
            # Whether we include it in the output does not matter,
            # Eclipse will ignore it
        enddate = schedule.dates[-1].date()
    else:
        enddate = sunschconf['enddate'] # datetime.date
        if type(enddate) != datetime.date:
            raise Exception("ERROR: end-date not in ISO-8601 format, must be YYYY-MM-DD")
        
    # Clip anything that is beyond the enddate
    for date in schedule.dates:
        if date.date() > enddate:
            schedule.delete(date)
            
    # Ensure that the end-date is actually mentioned in the Schedule
    # so that we know Eclipse will actually simulate until this date
    if enddate not in [x.date() for x in schedule.dates]:
        schedule.add_keywords(datetime_from_date(enddate), [''])
            
    # Dategrid is added at the end, in order to support
    # an implicit end-date
    if 'dategrid' in sunschconf:
        dates = dategrid(sunschconf['startdate'], enddate,
                         sunschconf['dategrid'])
        for date in dates:
            schedule.add_keywords(datetime_from_date(date), [""])

    return schedule

def dategrid(startdate, enddate, interval):
    """Return a list of datetimes at given interval


    Parameters
    ----------
    startdate: datetime.date
               First date in range
    enddate: datetime.date
             Last date in range
    interval: str
              Must be among: 'monthly', 'yearly', 'weekly',
              'biweekly', 'bimonthly'
    Return
    ------
    list of datetime.date. Includes start-date, might not include end-date
    """

    supportedintervals = ['monthly', 'yearly', 'weekly', 'biweekly',
                          'bimonthly']
    if interval not in supportedintervals:
        raise Exception("Unsupported dategrid interval \"" + interval + \
                        "\". Pick among " + \
                        ", ".join(supportedintervals))
    dates = [startdate]
    date = startdate + datetime.timedelta(days=1)
    startdateweekday = startdate.weekday()

    # Brute force implementation by looping over all possible
    # days. This is robust with respect to all possible date oddities,
    # but makes it difficult to support more interval types.
    while date <= enddate:
        if interval == 'monthly':
            if date.day == 1:
                dates.append(date)
        elif interval == 'bimonthly':
            if date.day == 1 and date.month % 2 == 1:
                dates.append(date)
        elif interval == 'weekly':
            if date.weekday() == startdateweekday:
                dates.append(date)
        elif interval == 'biweekly':
            weeknumber = date.isocalendar()[1]
            if date.weekday() == startdateweekday and weeknumber % 2 == 1:
                dates.append(date)
        elif interval == 'yearly':
            if date.day == 1 and date.month == 1:
                dates.append(date)
        elif interval == 'daily':
            dates.append(date)
        date += datetime.timedelta(days=1)
    return dates



# If we are called from command line:
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("config",
                        help="Config file in YAML format for Schedule merging")
    parser.add_argument("-o", "--output", type=str, default="",
                        help="Override output in yaml config. Use - for stdout")
    parser.add_argument("-q", "--quiet", action='store_true',
                        help="Mute output from script")
    args = parser.parse_args()

    if not args.quiet and args.output != "-":
        header.compose("sunsch",
                       "May 2018",
                       ["Håvard Berland"], ["havb@equinor.com"],
                       ["Access help with -h"],
                       "Generate Schedule files for Eclipse " + \
                       "from snippets (merging and insertions)")


    # Load YAML file:
    config = yaml.load(open(args.config))

    # Overrides:
    if args.output != "":
        config['output'] = args.output

    if 'output' not in config:
        config['output'] = "-"  # Write to stdout

    if args.output == "-":
        args.quiet = True

    schedule = process_sch_config(config, args.quiet)

    if config['output'] == "-" or 'output' not in config:
        print str(schedule)
    else:
        if not args.quiet:
            print("Writing Eclipse deck to " + config['output'])
        open(config['output'], 'w').write(str(schedule))
