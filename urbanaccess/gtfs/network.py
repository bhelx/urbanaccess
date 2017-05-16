from future.utils import raise_with_traceback
import logging as lg
import numpy as np
import pandas as pd
import time

# Note: The above imported logging funcs were modified from the OSMnx library
#       & used with permission from the author Geoff Boeing: log, get_logger
#       OSMnx repo: https://github.com/gboeing/osmnx/blob/master/osmnx/utils.py

from urbanaccess.utils import log, df_to_hdf5, hdf5_to_df
from urbanaccess.network import ua_network
from urbanaccess import config
from urbanaccess.gtfs.gtfsfeeds_dataframe import gtfsfeeds_dfs

pd.options.mode.chained_assignment = None


def create_transit_net(gtfsfeeds_dfs, day,
                       calendar_dates_lookup=None,
                       timerange=None,
                       overwrite_existing_stop_times_int=False,
                       use_existing_stop_times_int=False,
                       save_processed_gtfs=False,
                       save_dir=config.settings.data_folder,
                       save_filename=None):
    """
    Create a travel time weight network graph in units of
    minutes from GTFS data

    Parameters
    ----------
    gtfsfeeds_dfs : object
        gtfsfeeds_dfs object with dataframes of stops, routes, trips,
        stop_times, calendar, and stop_times_int (optional)
    day : {'friday','monday','saturday', 'sunday','thursday','tuesday',
    'wednesday'}
        day of the week to extract transit schedule from that
        corresponds to the day in the GTFS calendar
    calendar_dates_lookup : dict, optional
        dictionary of the lookup column (key) as a string and corresponding
        string (value) a s string or list of strings to use to subset trips
        using the calendar_dates dataframe. Search will be exact. If none,
        then the calendar_dates dataframe will not be used to select trips
        that are not in the calendar dataframe.
        Example: {'schedule_type' : 'WD'} or {'schedule_type' : ['WD','SU']}
    timerange : list
        time range to extract transit schedule from in a list with time
        1 and time 2. it is suggested the time range
        specified is large enough to allow for travel
        from one end of the transit network to the other but small enough
        to represent a relevant travel time period such as a 3 hour window
        for the AM Peak period. must follow format
        of a 24 hour clock for example: 08:00:00 or 17:00:00
    overwrite_existing_stop_times_int : bool
        if true, and if there is an existing stop_times_int
        dataframe stored in the gtfsfeeds_dfs object it will be
        overwritten
    use_existing_stop_times_int : bool
        if true, and if there is an existing stop_times_int
        dataframe for the same time period stored in the
        gtfsfeeds_dfs object it will be used instead of re-calculated
    save_processed_gtfs : bool
        if true, all processed gtfs dataframes will
        be stored to disk in a hdf5 file
    save_dir : str
        directory to save the hdf5 file
    save_filename : str
        name to save the hdf5 file as

    Returns
    -------
    ua_network : object
    ua_network.transit_edges : pandas.DataFrame
    ua_network.transit_nodes : pandas.DataFrame
    """
    start_time = time.time()

    # timerange related checks
    assert isinstance(timerange, list)
    time_error_statement = ('{} starttime and endtime are not in the '
                            'correct format. Format should be 24 '
                            'hour clock in following format: '
                            '08:00:00 or 17:00:00'.format(timerange))
    assert len(timerange) == 2, time_error_statement
    assert timerange[0] < timerange[1], time_error_statement

    for item in timerange:
        assert isinstance(item, str), time_error_statement
        assert len(item) == 8, time_error_statement
    
    tr_one = str(timerange[1][0:2])
    tr_two = str(timerange[0][0:2])
    tr_time_thresh = (int(tr_one) - int(tr_two))
    if tr_time_thresh > 3:
        log('WARNING: Time range passed: {} is a {} hour '
            'period. Long periods over 3 hours may take a '
            'significant amount of time to '
            'process.'.format(timerange, str(tr_time_thresh)), level=lg.WARNING)

    # boolean parameter related checks
    for ea in [overwrite_existing_stop_times_int,
               use_existing_stop_times_int,
               save_processed_gtfs]:
        assert isinstance(ea, bool)

    # no gtfs dataframes can be empty for this function to work
    any_gtfs_empty = (gtfsfeeds_df.trips.empty or 
                      gtfsfeeds_df.calendar.empty or 
                      gtfsfeeds_df.stop_times.empty or 
                      gtfsfeeds_df.stops.empty)
    if any_gtfs_empty:
        err_text = ('One of the gtfsfeeds_df object trips, calendar, '
                    'stops, or stop_times were found to be empty.')
        raise_with_traceback(ValueError(err_text))

    # cols we expect in the dataframe
    columns = ['route_id',
               'direction_id',
               'trip_id',
               'service_id',
               'unique_agency_id']

    if 'direction_id' not in gtfsfeeds_dfs.trips.columns:
        columns.remove('direction_id')
    calendar_selected_trips_df = _trip_schedule_selector(
        input_trips_df=gtfsfeeds_dfs.trips[columns],
        input_calendar_df=gtfsfeeds_dfs.calendar,
        input_calendar_dates_df=gtfsfeeds_dfs.calendar_dates,
        day=day,
        calendar_dates_lookup=calendar_dates_lookup)

    if gtfsfeeds_dfs.stop_times_int.empty or \
            overwrite_existing_stop_times_int or \
                    use_existing_stop_times_int == False:
        gtfsfeeds_dfs.stop_times_int = _interpolate_stop_times(
            stop_times_df=gtfsfeeds_dfs.stop_times,
            calendar_selected_trips_df=calendar_selected_trips_df,
            day=day)

        gtfsfeeds_dfs.stop_times_int = _time_difference(
            stop_times_df=gtfsfeeds_dfs.stop_times_int)

    # direction_id is conditional, though
    if 'direction_id' not in gtfsfeeds_df.trips.columns:
        columns.remove('direction_id')
    
    trips_subgdf = gtfsfeeds_df.trips[columns]
    calendar_selected_trips_df = tripschedualselector(
                                    input_trips_df=trips_subgdf,
                                    input_calendar_df=gtfsfeeds_df.calendar,
                                    day=day)

    # confirm if need to interpolate stop times and, if yes, then do so
    interpolate_trigger = (gtfsfeeds_df.stop_times_int.empty or 
                           overwrite_existing_stop_times_int or 
                           use_existing_stop_times_int == False)
    if interpolate_trigger:
        interp_stops = interpolatestoptimes(
                        stop_times_df=gtfsfeeds_df.stop_times,
                        calendar_selected_trips_df=calendar_selected_trips_df,
                        day=day)

        time_diff_interp_stops = timedifference(stop_times_df=interp_stops)
        gtfsfeeds_df.stop_times_int = time_diff_interp_stops

        # check if we want to save this output to hdf5 file
        if save_processed_gtfs:
            save_processed_gtfs_data(
                                gtfsfeeds_df=gtfsfeeds_df,
                                dir=save_dir,
                                filename=save_filename)

    if use_existing_stop_times_int:
        err_text = ('Existing stop_times_int is empty. '
                    'set use_existing_stop_times_int to False to create it.')
        assert gtfsfeeds_df.stop_times_int.empty == False, err_text

    selected_interpolated_stop_times_df = timeselector(
                                                df=gtfsfeeds_df.stop_times_int,
                                                starttime=timerange[0],
                                                endtime=timerange[1])

    # generate final edges dataframe for transit
    desired_stops_int_cols = ['unique_trip_id',
                              'stop_id',
                              'unique_stop_id',
                              'timediff',
                              'stop_sequence',
                              'unique_agency_id',
                              'trip_id']
    sub_stops_int = selected_interpolated_stop_times_df[desired_stops_int_cols]
    final_edge_table = format_transit_net_edge(stop_times_df=sub_stops_int)

    # now convert to desired time format and apply to global network value
    ua_network.transit_edges = convert_imp_time_units(
                                                df=final_edge_table,
                                                time_col='weight',
                                                convert_to='minutes')

    # similarly, finalize select transit network nodes and apply to network
    fin_sel_stops = stops_in_edge_table_selector(
                        input_stops_df=gtfsfeeds_df.stops,
                        input_stop_times_df=selected_interpolated_stop_times_df)
    ua_network.transit_nodes = format_transit_net_nodes(df=fin_sel_stops)

    # TODO: Undesirable to continually update networks.transit_edges
    #       Should reach a "final" state before updating (so fix above)
    ua_network.transit_edges = route_type_to_edge(
                                    transit_edge_df=ua_network.transit_edges,
                                    stop_time_df=gtfsfeeds_df.stop_times)

    ua_network.transit_edges = route_id_to_edge(
                                    transit_edge_df=ua_network.transit_edges,
                                    trips_df=gtfsfeeds_df.trips)


    # assign node and edge net type
    ua_network.transit_nodes['net_type'] = 'transit'
    ua_network.transit_edges['net_type'] = 'transit'

    # closing message logs
    fin_time_diff = time.time() - start_time
    success_msg = ('Successfully created transit network. Took '
                   '{:,.2f} seconds').format(fin_time_diff)
    log(success_msg)

    return ua_network


def _trip_schedule_selector(input_trips_df, input_calendar_df,
                            input_calendar_dates_df, day,
                            calendar_dates_lookup=None):
    """
    Select trips that run on a specific day

    Parameters
    ----------
    input_trips_df : pandas.DataFrame
        trips dataframe
    input_calendar_df : pandas.DataFrame
        calendar dataframe
    input_calendar_dates_df : pandas.DataFrame
        calendar_dates dataframe
    day : {'friday','monday','saturday','sunday','thursday','tuesday',
    'wednesday'}
        day of the week to extract transit schedule that corresponds to the
        day in the GTFS calendar
    calendar_dates_lookup : dict, optional
        dictionary of the lookup column (key) as a string and corresponding
        string (value) a s string or list of strings to use to subset trips
        using the calendar_dates dataframe. Search will be exact. If none,
        then the calendar_dates dataframe will not be used to select trips
        that are not in the calendar dataframe.
        Example: {'schedule_type' : 'WD'} or {'schedule_type' : ['WD','SU']}

    Returns
    -------
    calendar_selected_trips_df : pandas.DataFrame

    """
    start_time = time.time()

    valid_days = ['friday', 'monday', 'saturday', 'sunday',
                  'thursday', 'tuesday', 'wednesday']

    if day not in valid_days:
        raise ValueError('Incorrect day specified. Must be one of lowercase '
                         'strings: friday, monday, saturday, sunday, '
                         'thursday, tuesday, wednesday.')

    # check format of calendar_dates_lookup
    if calendar_dates_lookup is not None:
        if not isinstance(calendar_dates_lookup, dict):
            raise ValueError('calendar_dates_lookup parameter is not a dict')
        for key in calendar_dates_lookup.keys():
            if not isinstance(key, str):
                raise ValueError('calendar_dates_lookup key {} must be a '
                                 'string'.format(key))

            if isinstance(calendar_dates_lookup[key], str):
                value = [calendar_dates_lookup[key]]
            else:
                if not isinstance(calendar_dates_lookup[key], list):
                    raise ValueError(
                        'calendar_dates_lookup value {} must be a string or a '
                        'list of strings'.format(
                            calendar_dates_lookup[key]))
                else:
                    value = calendar_dates_lookup[key]

            for string in value:
                if not isinstance(string, str):
                    raise ValueError('{} must be a string'.format(value))

    # create unique service ids
    df_list = [input_trips_df, input_calendar_df, input_calendar_dates_df]

    for df in df_list:
        df['unique_service_id'] = (df['service_id'].str.cat(
                df['unique_agency_id'].astype('str'),
                sep='_'))

    # select service ids where day specified has a 1 = service runs on that day
    log('Using calendar to extract service_ids to select trips.')
    input_calendar_df = input_calendar_df[(input_calendar_df[day] == 1)]
    input_calendar_df = input_calendar_df[['unique_service_id']]
    log('{:,} service_ids were extracted from calendar'.format(len(
        input_calendar_df)))

    # generate information needed to tell user the status of their trips in
    # terms of service_ids in calendar and calendar_dates tables
    trips_in_calendar = input_trips_df.loc[input_trips_df[
        'unique_service_id'].isin(
        input_calendar_df['unique_service_id'])]
    trips_notin_calendar = input_trips_df.loc[~input_trips_df[
        'unique_service_id'].isin(input_calendar_df['unique_service_id'])]

    pct_trips_in_calendar = round(len(trips_in_calendar) / len(
        input_trips_df) * 100, 3)
    pct_trips_notin_calendar = round(len(trips_notin_calendar) / len(
        input_trips_df) * 100, 3)

    log(('{:,} trip(s) {:.3f} percent of {:,} total trip records were found '
         'in calendar').format(len(trips_in_calendar),
                               pct_trips_in_calendar,
                               len(input_trips_df)))

    if len(trips_notin_calendar) > 0 and calendar_dates_lookup is None:
        warning_msg = (
            'NOTE: {:,} trip(s) {:.3f} percent of {:,} total trip records '
            'were found to be in calendar_dates and not calendar. The '
            'calendar_dates_lookup parameter is None. In order to use the '
            'trips inside the calendar_dates dataframe it is suggested you '
            'specify a search parameter in the calendar_dates_lookup dict. '
            'This should only be done if you know the corresponding '
            'GTFS feed is using calendar_dates instead of calendar to '
            'specify the service_ids. When in doubt do not use the '
            'calendar_dates_lookup parameter.')
        log(warning_msg.format(
            len(trips_notin_calendar),
            pct_trips_notin_calendar,
            len(input_trips_df)), level=lg.WARNING)

    # look for service_ids inside of calendar_dates if calendar does not
    # supply enough service_ids to select trips by
    if len(trips_notin_calendar) > 0 and calendar_dates_lookup is not None:

        log('Using calendar_dates to supplement service_ids extracted from '
            'calendar to select trips.')

        subset_result_df = pd.DataFrame()

        for col_name_key, string_value in calendar_dates_lookup.items():
            if col_name_key not in input_calendar_dates_df.columns:
                raise ValueError(('{} column not found in calendar_dates '
                                  'dataframe').format(col_name_key))

            if col_name_key not in input_calendar_dates_df.select_dtypes(
                    include=[object]).columns:
                raise ValueError('{} column is not object type'.format(
                    col_name_key))

            if not isinstance(string_value, list):
                string_value = [string_value]

            for text in string_value:

                subset_result = input_calendar_dates_df[
                    input_calendar_dates_df[col_name_key].str.match(
                        text, case=False, na=False)]
                log(('Found {:,} records that matched query: column: {} and '
                     'string: {}').format(len(subset_result),
                                          col_name_key,
                                          text))

                subset_result_df = subset_result_df.append(subset_result)

        subset_result_df.drop_duplicates(inplace=True)
        subset_result_df = subset_result_df[['unique_service_id']]

        log('{:,} service_ids were extracted from calendar_dates'.format(len(
            subset_result_df)))
        input_calendar_df = input_calendar_df.append(subset_result_df)
        input_calendar_df.drop_duplicates(inplace=True)

    # select and create df of trips that match the service ids for the day of
    # the week specified merge calendar df that has service ids for
    # specified day with trips df
    calendar_selected_trips_df = input_trips_df.loc[
        input_trips_df['unique_service_id'].isin(
            input_calendar_df['unique_service_id'])]

    sort_columns = ['route_id', 'trip_id', 'direction_id']
    if 'direction_id' not in calendar_selected_trips_df.columns:
        sort_columns.remove('direction_id')
    calendar_selected_trips_df.sort_values(by=sort_columns, inplace=True)
    calendar_selected_trips_df.reset_index(drop=True, inplace=True)
    calendar_selected_trips_df.drop('unique_service_id', axis=1, inplace=True)

    if calendar_dates_lookup is None:
        log(('{:,} of {:,} total trips were extracted representing calendar '
             'day: {}. Took {:,.2f} seconds').format(len(
            calendar_selected_trips_df), len(input_trips_df),
            day, time.time() - start_time))
    else:
        log(('{:,} of {:,} total trips were extracted representing calendar '
             'day: {} and calendar_dates search parameters: {}. Took {:,'
             '.2f} seconds').format(len(
            calendar_selected_trips_df), len(input_trips_df),
            day, calendar_dates_lookup, time.time() - start_time))

    return calendar_selected_trips_df


def _interpolate_stop_times(stop_times_df, calendar_selected_trips_df, day):
    """
    Interpolate missing stop times using a linear
    interpolator between known stop times

    Parameters
    ----------
    stop_times_df : pandas.DataFrame
        stop times dataframe
    calendar_selected_trips_df : pandas.DataFrame
        dataframe of trips that run on specific day
    day : {'friday','monday','saturday','sunday','thursday','tuesday',
    'wednesday'}
        day of the week to extract transit schedule from that corresponds
        to the day in the GTFS calendar

    Returns
    -------
    final_stop_times_df : pandas.DataFrame

    """

    start_time = time.time()

    # create unique trip ids
    df_list = [calendar_selected_trips_df, stop_times_df]

    for df in df_list:
        df['unique_trip_id'] = (df['trip_id'].str.cat(
                df['unique_agency_id'].astype('str'),
                sep='_'))

    # sort stop times inplace based on first to last stop in
    # sequence -- required as the linear interpolator runs
    # from first value to last value
    if stop_times_df['stop_sequence'].isnull().sum() > 1:
        log('WARNING: There are {:,} '
            'stop_sequence records missing in the stop_times dataframe. '
            'Please check these missing values. In order for interpolation '
            'to proceed correctly, '
            'all records must have a stop_sequence value.'.format(
            stop_times_df['stop_sequence'].isnull().sum()), level=lg.WARNING)

    stop_times_df.sort_values(by=['unique_trip_id', 'stop_sequence'],
                              inplace=True)
    # make list of unique trip ids from the calendar_selected_trips_df
    uniquetriplist = calendar_selected_trips_df[
        'unique_trip_id'].unique().tolist()
    # select trip ids that match the trips in the
    # calendar_selected_trips_df -- resulting df will be stop times
    # only for trips that run on the service day of interest
    stop_times_df = stop_times_df[
        stop_times_df['unique_trip_id'].isin(uniquetriplist)]

    # count missing stop times
    missing_stop_times_count = stop_times_df[
        'departure_time_sec'].isnull().sum()

    # if there are stop times missing that need interpolation notify user
    if missing_stop_times_count > 0:

        log('Note: Processing may take a long time depending'
            ' on the number of records. '
            'Total unique trips to assess: {:,}'.format(
            len(stop_times_df['unique_trip_id'].unique())), level=lg.WARNING)
        log('Starting departure stop time interpolation...')
        log((
                'Departure time records missing from trips following {} '
                'schedule: {:,} ({:.2f} percent of {:,} total '
                'records)').format(
            day, missing_stop_times_count,
            (missing_stop_times_count / len(stop_times_df)) * 100,
            len(stop_times_df['departure_time_sec'])))

        log('Interpolating...')

    else:

        log('There are no departure time records missing from trips '
            'following {} schedule. There are no records to '
            'interpolate.'.format(day))

    # Find trips with more than one missing time
    # Note: all trip ids have at least 1 null departure time because the
    # last stop in a trip is always null
    null_times = stop_times_df[stop_times_df.departure_time_sec.isnull()]
    trips_with_null = null_times.unique_trip_id.value_counts()
    trips_with_more_than_one_null = trips_with_null[
        trips_with_null > 1].index.values

    # Subset stop times DataFrame to only those with >1 null time
    df_for_interpolation = stop_times_df.loc[
        stop_times_df.unique_trip_id.isin(trips_with_more_than_one_null)]

    if len(df_for_interpolation) > 0:

        # Pivot to DataFrame where each unique trip has its own column
        # Index is stop_sequence
        pivot = df_for_interpolation.pivot(index='stop_sequence',
                                           columns='unique_trip_id',
                                           values='departure_time_sec')

        # Interpolate on the whole DataFrame at once
        interpolator = pivot.interpolate(method='linear', axis=0,
                                         limit_direction='forward')

        # Melt back into stacked format
        interpolator['stop_sequence_merge'] = interpolator.index
        melted = pd.melt(interpolator, id_vars='stop_sequence_merge')
        melted.rename(columns={'value': 'departure_time_sec_interpolate'},
                      inplace=True)

        # Get the last valid stop for each unique trip,
        # to filter out trailing NaNs
        last_valid_stop_series = pivot.apply(
            lambda col: col.last_valid_index(), axis=0)
        last_valid_stop_df = last_valid_stop_series.to_frame('last_valid_stop')

        df_for_interpolation = (df_for_interpolation
                                .merge(last_valid_stop_df,
                                       left_on='unique_trip_id',
                                       right_index=True))
        trailing = (df_for_interpolation.stop_sequence >
                    df_for_interpolation.last_valid_stop)

        # Calculate a stop_sequence without trailing NaNs, to merge the correct
        # interpolated times back in
        df_for_interpolation['stop_sequence_merge'] = (
            df_for_interpolation[~trailing]['stop_sequence'])

        # Need to check if existing index in column names and drop if so (else
        # a ValueError where Pandas can't insert
        # b/c col already exists will occur)
        drop_bool = False
        if _check_if_index_name_in_cols(df_for_interpolation):
            # move the current index to own col named 'index'
            col_name_to_copy = df_for_interpolation.index.name
            col_to_copy = df_for_interpolation[col_name_to_copy].copy()
            df_for_interpolation['index'] = col_to_copy
            drop_bool = True
        df_for_interpolation.reset_index(inplace=True, drop=drop_bool)

        # Merge back into original index
        interpolated_df = pd.merge(df_for_interpolation, melted, 'left',
                                   on=['stop_sequence_merge',
                                       'unique_trip_id'])
        interpolated_df.set_index('index', inplace=True)
        interpolated_times = (
            interpolated_df[['departure_time_sec_interpolate']])

        final_stop_times_df = pd.merge(stop_times_df, interpolated_times,
                                       how='left', left_index=True,
                                       right_index=True, sort=False,
                                       copy=False)

    else:
        final_stop_times_df = stop_times_df
        final_stop_times_df['departure_time_sec_interpolate'] = (
            final_stop_times_df['departure_time_sec'])

    # fill in nulls in interpolated departure time column using trips that
    # did not need interpolation in order to create
    # one column with both original and interpolated times
    final_stop_times_df['departure_time_sec_interpolate'].fillna(
        final_stop_times_df['departure_time_sec'], inplace=True)

    if final_stop_times_df[
        'departure_time_sec_interpolate'].isnull().sum() > 0:
        log(
            'WARNING: Number of records unable to interpolate: {:,}. These '
            'records have been removed.'.format(
                final_stop_times_df[
                    'departure_time_sec_interpolate'].isnull().sum()),
            level=lg.WARNING)

    # convert the interpolated times (float) to integer so all times are
    # the same number format
    # first run int converter on non-null records (nulls here are the last
    # stop times in a trip because there is no departure)
    final_stop_times_df = final_stop_times_df[
        final_stop_times_df['departure_time_sec_interpolate'].notnull()]
    # convert float to int
    final_stop_times_df['departure_time_sec_interpolate'] = \
    final_stop_times_df['departure_time_sec_interpolate'].astype(int)

    # add unique stop id
    final_stop_times_df['unique_stop_id'] = final_stop_times_df[
        ['stop_id', 'unique_agency_id']].apply(
        lambda x: '{}_{}'.format(x[0], x[1]), axis=1)

    if missing_stop_times_count > 0:
        log(
            'Departure stop time interpolation complete. Took {:,'
            '.2f} seconds'.format(
                time.time() - start_time))

    return final_stop_times_df


def _time_difference(stop_times_df=None):
    """
    Calculate the difference in departure_time between stops in stop times
    table to produce travel time

    Parameters
    ----------
    stop_times_df : pandas.DataFrame
        interpolated stop times dataframe

    Returns
    -------
    stop_times_df : pandas.DataFrame

    """
    start_time = time.time()

    # calculate difference between consecutive records grouping by trip id.
    stop_times_df['timediff'] = stop_times_df.groupby('unique_trip_id')[
        'departure_time_sec_interpolate'].diff()
    log(
        'Difference between stop times has been successfully calculated. '
        'Took {:,.2f} seconds'.format(
            time.time() - start_time))

    return stop_times_df


def _time_selector(df=None, starttime=None, endtime=None):
    """
    Select stop times that fall within a specified time range

    Parameters
    ----------
    df : pandas.DataFrame
        interpolated stop times dataframe
    starttime : str
        24 hour clock formatted time 1
    endtime : str
        24 hour clock formatted time 2
    Returns
    -------
    selected_stop_timesdf : pandas.DataFrame

    """
    start_time = time.time()

    # takes input start and end time range from 24 hour clock and converts
    # it to seconds past midnight
    # in order to select times that may be after midnight

    # convert string time components to integer and then calculate seconds
    # past midnight
    # convert starttime 24 hour to seconds past midnight
    start_h = int(str(starttime[0:2]))
    start_m = int(str(starttime[3:5]))
    start_s = int(str(starttime[6:8]))
    starttime_sec = (start_h * 60 * 60) + (start_m * 60) + start_s
    # convert endtime 24 hour to seconds past midnight
    end_h = int(str(endtime[0:2]))
    end_m = int(str(endtime[3:5]))
    end_s = int(str(endtime[6:8]))
    endtime_sec = (end_h * 60 * 60) + (end_m * 60) + end_s

    # create df of stops times that are within the requested range
    selected_stop_timesdf = df[(
    (starttime_sec < df["departure_time_sec_interpolate"]) & (
    df["departure_time_sec_interpolate"] < endtime_sec))]

    log(
        'Stop times from {} to {} successfully selected {:,} records out of '
        '{:,} total records ({:.2f} percent of total). Took {:,'
        '.2f} seconds'.format(
            starttime, endtime, len(selected_stop_timesdf), len(df),
            (len(selected_stop_timesdf) / len(df)) * 100,
            time.time() - start_time))

    return selected_stop_timesdf


def _format_transit_net_edge(stop_times_df=None):
    """
    Format transit network data table to match the format required for edges
    in Pandana graph networks edges

    Parameters
    ----------
    stop_times_df : pandas.DataFrame
        interpolated stop times with travel time between stops for the subset
        time and day

    Returns
    -------
    merged_edge_df : pandas.DataFrame

    """
    start_time = time.time()

    log('Starting transformation process for {:,} '
        'total trips...'.format(len(stop_times_df['unique_trip_id'].unique())))

    # set columns for new df for data needed by pandana for edges
    merged_edge = []

    stop_times_df.sort_values(by=['unique_trip_id', 'stop_sequence'],
                              inplace=True)

    for trip, tmp_trip_df in stop_times_df.groupby(['unique_trip_id']):
        edge_df = pd.DataFrame({
            "node_id_from": tmp_trip_df['unique_stop_id'].iloc[:-1].values,
            "node_id_to": tmp_trip_df['unique_stop_id'].iloc[1:].values,
            "weight": tmp_trip_df['timediff'].iloc[1:].values,
            "unique_agency_id": tmp_trip_df['unique_agency_id'].iloc[
                                1:].values,
            # set unique trip id without edge order to join other data later
            "unique_trip_id": trip
        })

        # Set current trip id to edge id column adding edge order at
        # end of string
        edge_df['sequence'] = (edge_df.index + 1).astype(int)

        # append completed formatted edge table to master edge table
        merged_edge.append(edge_df)

    merged_edge_df = pd.concat(merged_edge, ignore_index=True)
    merged_edge_df['sequence'] = merged_edge_df['sequence'].astype(int,
                                                                   copy=False)
    merged_edge_df['id'] = merged_edge_df[
        ['unique_trip_id', 'sequence']].apply(
        lambda x: '{}_{}'.format(x[0], x[1]), axis=1)

    log('stop time table transformation to '
        'Pandana format edge table completed. '
        'Took {:,.2f} seconds'.format(time.time() - start_time))

    return merged_edge_df


def _convert_imp_time_units(df=None, time_col='weight', convert_to='minutes'):
    """
    Convert the travel time impedance units

    Parameters
    ----------
    df : pandas.DataFrame
        edge dataframe with weight column
    time_col : str
        name of column that holds the travel impedance
    convert_to : {'seconds','minutes'}
        unit to convert travel time to. should always be set to 'minutes'

    Returns
    -------
    df : pandas.DataFrame

    """
    valid_convert_to = ['seconds', 'minutes']
    assert convert_to in valid_convert_to and isinstance(convert_to, str)

    if convert_to == 'seconds':
        df[time_col] = df[time_col].astype('float')
        df[time_col] = df[time_col] * 60
        log('Time conversion completed: minutes converted to seconds.')

    if convert_to == 'minutes':
        df[time_col] = df[time_col].astype('float')
        df[time_col] = df[time_col] / 60.0
        log('Time conversion completed: seconds converted to minutes.')

    return df


def _stops_in_edge_table_selector(input_stops_df=None,
                                  input_stop_times_df=None):
    """
    Select stops that are active during the day and time period specified

    Parameters
    ----------
    input_stops_df : pandas.DataFrame
        stops dataframe
    input_stop_times_df : pandas.DataFrame
        stop_times dataframe

    Returns
    -------
    selected_stops_df : pandas.DataFrame

    """
    start_time = time.time()

    # add unique stop id
    input_stops_df['unique_stop_id'] = input_stops_df[
        ['stop_id', 'unique_agency_id']].apply(
        lambda x: '{}_{}'.format(x[0], x[1]), axis=1)

    # Select stop ids that match stop ids in the subset stop time data that
    # match day and time selection
    selected_stops_df = input_stops_df.loc[
        input_stops_df['unique_stop_id'].isin(
            input_stop_times_df['unique_stop_id'])]

    log(
        '{:,} of {:,} records selected from stops. Took {:,'
        '.2f} seconds'.format(
            len(selected_stops_df), len(input_stops_df),
            time.time() - start_time))

    return selected_stops_df


def _format_transit_net_nodes(df=None):
    """
    Create transit node table from stops dataframe and perform final formatting

    Parameters
    ----------
    df : pandas.DataFrame
        transit node dataframe

    Returns
    -------
    final_node_df : pandas.DataFrame

    """
    start_time = time.time()

    # add unique stop id
    if 'unique_stop_id' not in df.columns:
        df['unique_stop_id'] = df[['stop_id', 'unique_agency_id']].apply(
            lambda x: '{}_{}'.format(x[0], x[1]), axis=1)

    final_node_df = pd.DataFrame()
    final_node_df['node_id'] = df['unique_stop_id']
    final_node_df['x'] = df['stop_lon']
    final_node_df['y'] = df['stop_lat']

    # keep useful info from stops table
    col_list = ['unique_agency_id', 'route_type', 'stop_id', 'stop_name']
    # if these optional cols exist then keep those that do
    optional_gtfs_cols = ['parent_station', 'stop_code', 'wheelchair_boarding',
                          'zone_id', 'location_type']
    for item in optional_gtfs_cols:
        if item in df.columns:
            col_list.append(item)

    final_node_df = pd.concat([final_node_df, df[col_list]], axis=1)
    # set node index to be unique stop id
    final_node_df = final_node_df.set_index('node_id')

    log(
        'stop time table transformation to Pandana format node table '
        'completed. Took {:,.2f} seconds'.format(
            time.time() - start_time))

    return final_node_df


def _route_type_to_edge(transit_edge_df=None, stop_time_df=None):
    """
    Append route type information to transit edge table

    Parameters
    ----------
    transit_edge_df : pandas.DataFrame
        transit edge dataframe
    stop_time_df : pandas.DataFrame
        stop time dataframe

    Returns
    -------
    transit_edge_df_w_routetype : pandas.DataFrame

    """
    start_time = time.time()

    # create unique trip ids
    stop_time_df['unique_trip_id'] = stop_time_df[
        ['trip_id', 'unique_agency_id']].apply(
        lambda x: '{}_{}'.format(x[0], x[1]), axis=1)

    # join route_id to the edge table
    merged_df = pd.merge(transit_edge_df,
                         stop_time_df[['unique_trip_id', 'route_type']],
                         how='left', on='unique_trip_id', sort=False,
                         copy=False)
    merged_df.drop_duplicates(subset='unique_trip_id', keep='first', inplace
    =True)  # need to get unique records here to have a one to one join -
    # this serves as the look up table
    # join the look up table created above to the table of interest
    transit_edge_df_w_routetype = pd.merge(transit_edge_df, merged_df[
        ['route_type', 'unique_trip_id']], how='left', on='unique_trip_id',
                                           sort=False, copy=False)

    log(
        'route type successfully joined to transit edges. Took {:,'
        '.2f} seconds'.format(
            time.time() - start_time))

    return transit_edge_df_w_routetype


def _route_id_to_edge(transit_edge_df=None, trips_df=None):
    """
    Append route ids to transit edge table

    Parameters
    ----------
    transit_edge_df : pandas.DataFrame
        transit edge dataframe
    trips_df : pandas.DataFrame
        trips dataframe

    Returns
    -------
    transit_edge_df_with_routes : pandas.DataFrame

    """
    start_time = time.time()

    if 'unique_route_id' not in transit_edge_df.columns:
        # create unique trip and route ids
        trips_df['unique_trip_id'] = trips_df[
            ['trip_id', 'unique_agency_id']].apply(
            lambda x: '{}_{}'.format(x[0], x[1]), axis=1)
        trips_df['unique_route_id'] = trips_df[
            ['route_id', 'unique_agency_id']].apply(
            lambda x: '{}_{}'.format(x[0], x[1]), axis=1)

        transit_edge_df_with_routes = pd.merge(transit_edge_df, trips_df[
            ['unique_trip_id', 'unique_route_id']],
                                               how='left',
                                               on='unique_trip_id', sort=False,
                                               copy=False)

    log(
        'route id successfully joined to transit edges. Took {:,'
        '.2f} seconds'.format(
            time.time() - start_time))

    return transit_edge_df_with_routes


def edge_impedance_by_route_type(transit_edge_df=None,
                                 street_level_rail=None,
                                 underground_rail=None,
                                 intercity_rail=None,
                                 bus=None,
                                 ferry=None,
                                 cable_car=None,
                                 gondola=None,
                                 funicular=None):
    """
    Penalize transit edge travel time based on transit mode type

    Parameters
    ----------
    transit_edge_df : pandas.DataFrame
        transit edge dataframe
    street_level_rail : float
        factor between -1 to 1 to multiply against travel time
    intercity_rail : float
        factor between -1 to 1 to multiply against travel time
    bus : float
        factor between -1 to 1 to multiply against travel time
    ferry : float
        factor between -1 to 1 to multiply against travel time
    cable_car : float
        factor between -1 to 1 to multiply against travel time
    gondola : float
        factor between -1 to 1 to multiply against travel time
    funicular : float
        factor between -1 to 1 to multiply against travel time

    Returns
    -------
    ua_network : object
    ua_network.transit_edges : pandas.DataFrame

    """
    assert 'route_type' in transit_edge_df.columns, 'No route_type column ' \
                                                    'was found in dataframe'

    # check count of records for each route type
    route_type_desc = {0: 'Street Level Rail: Tram Streetcar Light rail',
                       1: 'Underground rail: Subway or Metro',
                       2: 'Rail: intercity or long-distance ', 3: 'Bus',
                       4: 'Ferry', 5: 'Cable Car',
                       6: 'Gondola or Suspended cable car',
                       7: 'Steep incline: Funicular'}
    log('Route type distribution as percentage of '
        'transit mode: {:.2f}'.format(
        transit_edge_df['route_type'].map(route_type_desc.get).value_counts(
            normalize=True, dropna=False) * 100))

    var_list = [street_level_rail, underground_rail, intercity_rail, bus,
                ferry, cable_car, gondola, funicular]

    for var in var_list:
        if var is not None:
            assert isinstance(var,
                              float), 'One or more variables are not float'

    travel_time_col_name = 'weight'
    travel_time_col = transit_edge_df[travel_time_col_name]

    if street_level_rail is not None and len(
            transit_edge_df[transit_edge_df['route_type'] == 0]) > 0:
        transit_edge_df[travel_time_col_name][
            transit_edge_df['route_type'] == 0] = travel_time_col + (
        travel_time_col * street_level_rail)
        log(
            'Adjusted Street Level Rail transit edge impedance based on mode'
            ' type penalty coefficient: {}'.format(
                street_level_rail))
    if underground_rail is not None and len(
            transit_edge_df[transit_edge_df['route_type'] == 1]) > 0:
        transit_edge_df[travel_time_col_name][
            transit_edge_df['route_type'] == 1] = travel_time_col + (
        travel_time_col * underground_rail)
        log(
            'Adjusted Underground rail transit edge impedance based on mode '
            'type penalty coefficient: {}'.format(
                underground_rail))
    if intercity_rail is not None and len(
            transit_edge_df[transit_edge_df['route_type'] == 2]) > 0:
        transit_edge_df[travel_time_col_name][
            transit_edge_df['route_type'] == 2] = travel_time_col + (
        travel_time_col * intercity_rail)
        log(
            'Adjusted Rail transit edge impedance based on mode type penalty '
            'coefficient: {}'.format(
                intercity_rail))
    if bus is not None and len(
            transit_edge_df[transit_edge_df['route_type'] == 3]) > 0:
        transit_edge_df[travel_time_col_name][
            transit_edge_df['route_type'] == 3] = travel_time_col + (
        travel_time_col * bus)
        log(
            'Adjusted Bus transit edge impedance based on mode type penalty '
            'coefficient: {}'.format(
                bus))
    if ferry is not None and len(
            transit_edge_df[transit_edge_df['route_type'] == 4]) > 0:
        transit_edge_df[travel_time_col_name][
            transit_edge_df['route_type'] == 4] = travel_time_col + (
        travel_time_col * ferry)
        log(
            'Adjusted Ferry transit edge impedance based on mode type '
            'penalty coefficient: {}'.format(
                ferry))
    if cable_car is not None and len(
            transit_edge_df[transit_edge_df['route_type'] == 5]) > 0:
        transit_edge_df[travel_time_col_name][
            transit_edge_df['route_type'] == 5] = travel_time_col + (
        travel_time_col * cable_car)
        log(
            'Adjusted Cable Car transit edge impedance based on mode type '
            'penalty coefficient: {}'.format(
                cable_car))
    if gondola is not None and len(
            transit_edge_df[transit_edge_df['route_type'] == 6]) > 0:
        transit_edge_df[travel_time_col_name][
            transit_edge_df['route_type'] == 6] = travel_time_col + (
        travel_time_col * gondola)
        log(
            'Adjusted Gondola or Suspended cable car transit edge impedance '
            'based on mode type penalty coefficient: {}'.format(
                gondola))
    if funicular is not None and len(
            transit_edge_df[transit_edge_df['route_type'] == 7]) > 0:
        transit_edge_df[travel_time_col_name][
            transit_edge_df['route_type'] == 7] = travel_time_col + (
        travel_time_col * funicular)
        log(
            'Adjusted Funicular transit edge impedance based on mode type '
            'penalty coefficient: {}'.format(
                funicular))

    ua_network.transit_edges = transit_edge_df

    log('Transit edge impedance mode type penalty calculation complete')

    return ua_network


def save_processed_gtfs_data(gtfsfeeds_dfs=None,
                             dir=config.settings.data_folder,
                             filename=None):
    """
    Write dataframes in a gtfsfeeds_dfs object to a hdf5 file

    Parameters
    ----------
    gtfsfeeds_dfs : object
        gtfsfeeds_dfs object
    dir : string, optional
        directory to save hdf5 file
    filename : string
        name of the hdf5 file to save with .h5 extension

    Returns
    -------
    None
    """
    assert gtfsfeeds_dfs is not None \
           or gtfsfeeds_dfs.stops.empty == False \
           or gtfsfeeds_dfs.routes.empty == False \
           or gtfsfeeds_dfs.trips.empty == False \
           or gtfsfeeds_dfs.stop_times.empty == False \
           or gtfsfeeds_dfs.calendar.empty == False \
           or gtfsfeeds_dfs.stop_times_int.empty == False, 'gtfsfeeds_dfs is ' \
                                                           '' \
                                                           'missing one of ' \
                                                           'the required ' \
                                                           'dataframes.'

    df_to_hdf5(data=gtfsfeeds_dfs.stops, key='stops', overwrite_key=False,
               dir=dir, filename=filename, overwrite_hdf5=False)
    df_to_hdf5(data=gtfsfeeds_dfs.routes, key='routes', overwrite_key=False,
               dir=dir, filename=filename, overwrite_hdf5=False)
    df_to_hdf5(data=gtfsfeeds_dfs.trips, key='trips', overwrite_key=False,
               dir=dir, filename=filename, overwrite_hdf5=False)
    df_to_hdf5(data=gtfsfeeds_dfs.stop_times, key='stop_times',
               overwrite_key=False, dir=dir, filename=filename,
               overwrite_hdf5=False)
    df_to_hdf5(data=gtfsfeeds_dfs.calendar, key='calendar',
               overwrite_key=False, dir=dir, filename=filename,
               overwrite_hdf5=False)
    df_to_hdf5(data=gtfsfeeds_dfs.stop_times_int, key='stop_times_int',
               overwrite_key=False, dir=dir, filename=filename,
               overwrite_hdf5=False)

    if gtfsfeeds_dfs.headways.empty == False:
        df_to_hdf5(data=gtfsfeeds_dfs.headways, key='headways',
                   overwrite_key=False, dir=dir, filename=filename,
                   overwrite_hdf5=False)

    if gtfsfeeds_dfs.calendar_dates.empty == False:
        df_to_hdf5(data=gtfsfeeds_dfs.calendar_dates, key='calendar_dates',
                   overwrite_key=False, dir=dir, filename=filename,
                   overwrite_hdf5=False)


def load_processed_gtfs_data(dir=config.settings.data_folder, filename=None):
    """
    Read data from a hdf5 file to a gtfsfeeds_dfs object

    Parameters
    ----------
    dir : string, optional
        directory to read hdf5 file
    filename : string
        name of the hdf5 file to read with .h5 extension

    Returns
    -------
    gtfsfeeds_dfs : object
    """
    gtfsfeeds_dfs.stops = hdf5_to_df(dir=dir, filename=filename, key='stops')
    gtfsfeeds_dfs.routes = hdf5_to_df(dir=dir, filename=filename, key='routes')
    gtfsfeeds_dfs.trips = hdf5_to_df(dir=dir, filename=filename, key='trips')
    gtfsfeeds_dfs.stop_times = hdf5_to_df(dir=dir, filename=filename,
                                          key='stop_times')
    gtfsfeeds_dfs.calendar = hdf5_to_df(dir=dir, filename=filename,
                                        key='calendar')
    gtfsfeeds_dfs.stop_times_int = hdf5_to_df(dir=dir, filename=filename,
                                              key='stop_times_int')

    hdf5_load_path = '{}/{}'.format(dir, filename)
    with pd.HDFStore(hdf5_load_path) as store:

        if 'headways' in store.keys():
            gtfsfeeds_dfs.headways = hdf5_to_df(dir=dir,
                                                filename=filename,
                                                key='headways')
        if 'calendar_dates' in store.keys():
            gtfsfeeds_dfs.calendar_dates = hdf5_to_df(dir=dir,
                                                      filename=filename,
                                                      key='calendar_dates')

    return gtfsfeeds_dfs


def _check_if_index_name_in_cols(df):
    """
    Check if existing index is in the passed dataframe list of column names

    Parameters
    ----------
    df : pandas.DataFrame
        interpolated stop_time dataframe

    Returns
    -------
    iname : tuple
    """
    cols = df.columns.values
    iname = df.index.name
    return (iname in cols)