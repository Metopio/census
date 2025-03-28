import warnings
from functools import wraps, lru_cache

import pkg_resources
__version__ = pkg_resources.require("census")[0].version

ALL = '*'


def new_session(*args, **kwargs):
    import requests
    return requests.session(*args, **kwargs)


class APIKeyError(Exception):
    """ Invalid API key
    """

    def __init__(self, value):
        self.value = value

    def __str__(self):
        return repr(self.value)


def list_or_str(v):
    """ Convert a single value into a list.
    """
    if isinstance(v, (list, tuple)):
        return v
    return [v]


def float_or_str(v):
    try:
        return float(v)
    except ValueError:
        return str(v)


def supported_years(*years):
    def inner(func):
        @wraps(func)
        def wrapper(self, *args, **kwargs):
            year = kwargs.get('year', self.default_year)
            _years = years if years else self.years
            if int(year) not in _years:
                raise UnsupportedYearException(
                    'Geography is not available in {}. Available years include {}'.format(year, _years))
            return func(self, *args, **kwargs)
        return wrapper
    return inner


def retry_on_transient_error(func):

    def wrapper(self, *args, **kwargs):
        for _ in range(max(self.retries - 1, 0)):
            try:
                result = func(self, *args, **kwargs)
            except CensusException as e:
                if "There was an error while running your query.  We've logged the error and we'll correct it ASAP.  Sorry for the inconvenience." in str(e):
                    pass
                else:
                    raise
            else:
                return result
        else:
            return func(self, *args, **kwargs)

    return wrapper


def chunks(l, n):
    """Yield successive n-sized chunks from l."""
    for i in range(0, len(l), n):
        yield l[i:i + n]


def merge(dicts):
    return dict(item for d in dicts for item in d.items())


class CensusException(Exception):
    pass


class UnsupportedYearException(CensusException):
    pass


class Client(object):
    endpoint_url = 'https://api.census.gov/data/%s/%s'
    definitions_url = 'https://api.census.gov/data/%s/%s/variables.json'
    definition_url = 'https://api.census.gov/data/%s/%s/variables/%s.json'
    groups_url = 'https://api.census.gov/data/%s/%s/groups.json'

    def __init__(self, key, year=None, session=None, retries=3):
        self._key = key
        self.session = session or new_session()
        if year:
            self.default_year = year
        self.retries = retries

    def tables(self, year=None):
        """
        Returns a list of the data tables available from this source.
        """
        # Set the default year if one hasn't been passed
        if year is None:
            year = self.default_year

        # Query the table metadata as raw JSON
        tables_url = self.groups_url % (year, self.dataset)
        resp = self.session.get(tables_url)

        # Pass it out
        return resp.json()['groups']

    @supported_years()
    def fields(self, year=None, flat=False):
        if year is None:
            year = self.default_year

        data = {}

        fields_url = self.definitions_url % (year, self.dataset)

        resp = self.session.get(fields_url)
        obj = resp.json()

        if flat:

            for key, elem in obj['variables'].items():
                if key in ['for', 'in']:
                    continue
                data[key] = "{}: {}".format(elem['concept'], elem['label'])

        else:

            data = obj['variables']
            if 'for' in data:
                data.pop("for", None)
            if 'in' in data:
                data.pop("in", None)

        return data

    def get(self, fields, geo, year=None, **kwargs):
        """
        The API only accepts up to 50 fields on each query.
        Chunk requests, and use the unique GEO_ID to match up the chunks
        in case the responses are in different orders.
        GEO_ID is not reliably present in pre-2010 requests.
        """
        sort_by_geoid = len(fields) > 49 and (not year or year > 2009)
        all_results = (self.query(forty_nine_fields, geo, year, sort_by_geoid=sort_by_geoid, **kwargs)
                       for forty_nine_fields in chunks(fields, 49))
        merged_results = [merge(result) for result in zip(*all_results)]

        return merged_results

    @retry_on_transient_error
    def query(self, fields, geo, year=None, sort_by_geoid=False, **kwargs):
        if year is None:
            year = self.default_year

        fields = list_or_str(fields)
        if sort_by_geoid:
            if isinstance(fields, list):
                fields += ['GEO_ID']
            elif isinstance(fields, tuple):
                fields += ('GEO_ID',)

        url = self.endpoint_url % (year, self.dataset)

        params = {
            'get': ",".join(fields),
            'for': geo['for'],
            'key': self._key,
        }

        if 'in' in geo:
            params['in'] = geo['in']

        resp = self.session.get(url, params=params)

        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError as ex:
                if '<title>Invalid Key</title>' in resp.text:
                    raise APIKeyError(' '.join(resp.text.splitlines()))
                else:
                    raise ex

            headers = data.pop(0)
            types = [self._field_type(header, year) for header in headers]
            results = [{header: (cast(item) if item is not None else None)
                        for header, cast, item
                        in zip(headers, types, d)}
                       for d in data]
            if sort_by_geoid:
                if 'GEO_ID' in fields:
                    results = sorted(results, key=lambda x: x['GEO_ID'])
                else:
                    results = sorted(results, key=lambda x: x.pop('GEO_ID'))
            return results

        elif resp.status_code == 204:
            return []

        else:
            raise CensusException(resp.text)

    @lru_cache(maxsize=1024)
    def _field_type(self, field, year):
        url = self.definition_url % (year, self.dataset, field)
        resp = self.session.get(url)

        types = {"fips-for": str,
                 "fips-in": str,
                 "int": float_or_str,
                 "float": float,
                 "string": str}

        if resp.status_code == 200:
            predicate_type = resp.json().get("predicateType", "string")
            return types[predicate_type]
        else:
            return str

    @supported_years()
    def us(self, fields, **kwargs):
        return self.get(fields, geo={'for': 'us:1'}, **kwargs)

    @supported_years()
    def state(self, fields, state_fips, **kwargs):
        return self.get(fields, geo={
            'for': 'state:{}'.format(state_fips),
        }, **kwargs)

    @supported_years()
    def state_county(self, fields, state_fips, county_fips, **kwargs):
        return self.get(fields, geo={
            'for': 'county:{}'.format(county_fips),
            'in': 'state:{}'.format(state_fips),
        }, **kwargs)

    @supported_years()
    def state_place(self, fields, state_fips, place, **kwargs):
        return self.get(fields, geo={
            'for': 'place:{}'.format(place),
            'in': 'state:{}'.format(state_fips),
        }, **kwargs)

    @supported_years()
    def state_district(self, fields, state_fips, district, **kwargs):
        warnings.warn(
            "state_district refers to congressional districts; use state_congressional_district instead",
            DeprecationWarning
        )

        # throwaway, but we can't pass it in twice.
        kwargs.pop('congressional_district', None)

        return self.state_congressional_district(fields, state_fips, district, **kwargs)

    @supported_years()
    def state_congressional_district(self, fields, state_fips, congressional_district, **kwargs):
        return self.get(fields, geo={
            'for': 'congressional district:{}'.format(congressional_district),
            'in': 'state:{}'.format(state_fips),
        }, **kwargs)

    @supported_years()
    def state_legislative_district_upper(self, fields, state_fips, legislative_district, **kwargs):
        return self.get(fields, geo={
            'for': 'state legislative district (upper chamber):{}'.format(str(legislative_district).zfill(3)),
            'in': 'state:{}'.format(state_fips),
        }, **kwargs)

    @supported_years()
    def state_legislative_district_lower(self, fields, state_fips, legislative_district, **kwargs):
        return self.get(fields, geo={
            'for': 'state legislative district (lower chamber):{}'.format(str(legislative_district).zfill(3)),
            'in': 'state:{}'.format(state_fips),
        }, **kwargs)


class ACSClient(Client):

    def _switch_endpoints(self, year):

        if year > 2009:
            self.endpoint_url = 'https://api.census.gov/data/%s/acs/%s'
            self.definitions_url = 'https://api.census.gov/data/%s/acs/%s/variables.json'
            self.definition_url = 'https://api.census.gov/data/%s/acs/%s/variables/%s.json'
            self.groups_url = 'https://api.census.gov/data/%s/acs/%s/groups.json'
        else:
            self.endpoint_url = super(ACSClient, self).endpoint_url
            self.definitions_url = super(ACSClient, self).definitions_url
            self.definition_url = super(ACSClient, self).definition_url
            self.groups_url = super(ACSClient, self).groups_url

    def tables(self, *args, **kwargs):
        self._switch_endpoints(kwargs.get('year', self.default_year))
        return super(ACSClient, self).tables(*args, **kwargs)

    def get(self, *args, **kwargs):
        self._switch_endpoints(kwargs.get('year', self.default_year))

        return super(ACSClient, self).get(*args, **kwargs)


class ACS5Client(ACSClient):

    default_year = 2023
    dataset = 'acs5'

    years = (2023, 2022, 2021, 2020, 2019, 2018, 2017, 2016, 2015, 2014, 2013, 2012, 2011, 2010, 2009)

    @supported_years()
    def state_county_subdivision(self, fields, state_fips,
                                 county_fips, subdiv_fips, **kwargs):
        return self.get(fields, geo={
            'for': 'county subdivision:{}'.format(subdiv_fips),
            'in': 'state:{} county:{}'.format(state_fips, county_fips),
        }, **kwargs)

    @supported_years()
    def state_county_tract(self, fields, state_fips,
                           county_fips, tract, **kwargs):
        return self.get(fields, geo={
            'for': 'tract:{}'.format(tract),
            'in': 'state:{} county:{}'.format(state_fips, county_fips),
        }, **kwargs)

    @supported_years()
    def state_county_blockgroup(self, fields, state_fips, county_fips,
                                blockgroup, tract=None, **kwargs):
        geo = {
            'for': 'block group:{}'.format(blockgroup),
            'in': 'state:{} county:{}'.format(state_fips, county_fips),
        }
        if tract:
            geo['in'] += ' tract:{}'.format(tract)
        return self.get(fields, geo=geo, **kwargs)

    @supported_years(2023, 2022, 2021, 2020, 2019, 2018, 2017, 2016, 2015, 2014, 2013, 2012, 2011)
    def zipcode(self, fields, zcta, **kwargs):
        warnings.warn(
            "zipcode has been deprecated; use state_zipcode instead",
            DeprecationWarning
        )

        state_fips = kwargs.pop('state_fips')

        return self.state_zipcode(fields, state_fips, zcta, **kwargs)

    @supported_years(2023, 2022, 2021, 2020, 2019, 2018, 2017, 2016, 2015, 2014, 2013, 2012, 2011)
    def state_zipcode(self, fields, state_fips, zcta, **kwargs):
        year = kwargs.get('year', self.default_year)
        geo = {
            'for': 'zip code tabulation area:{}'.format(zcta),
        }
        # for 2020 onward, we need to use "regionin" instead of "in" due to ZCTA's being nested under states 
        # For 2022+, we can call the whole country again??
        if year >= 2022:
            pass
        elif year < 2020:
            geo["in"] = 'state:{}'.format(state_fips)
        else:
            geo["regionin"] = 'state:{}'.format(state_fips)
        return self.get(fields, geo, **kwargs)
    
    @supported_years(2023, 2022, 2021, 2020)
    def state_elementary_school(self, fields, state_fips,
                           fips, **kwargs):
        if not state_fips:
            return self.get(fields, geo={'for': 'school district (elementary):{}'.format(fips)}, **kwargs)
        return self.get(fields, geo={
            'for': 'school district (elementary):{}'.format(fips),
            'in': 'state:{}'.format(state_fips),
        }, **kwargs)
    
    @supported_years(2023, 2022, 2021, 2020)
    def state_secondary_school(self, fields, state_fips,
                           fips, **kwargs):
        if not state_fips:
            return self.get(fields, geo={'for': 'school district (secondary):{}'.format(fips)}, **kwargs)
        return self.get(fields, geo={
            'for': 'school district (secondary):{}'.format(fips),
            'in': 'state:{}'.format(state_fips),
        }, **kwargs)
    
    @supported_years(2023, 2022, 2021, 2020)
    def state_unified_school(self, fields, state_fips,
                           fips, **kwargs):
        if not state_fips:
            return self.get(fields, geo={'for': 'school district (unified):{}'.format(fips)}, **kwargs)
        return self.get(fields, geo={
            'for': 'school district (unified):{}'.format(fips),
            'in': 'state:{}'.format(state_fips),
        }, **kwargs)

class ACS5DpClient(ACS5Client):

    dataset = 'acs5/profile'


class ACS5StClient(ACS5Client):
    def _switch_endpoints(self, year):
        self.endpoint_url = 'https://api.census.gov/data/%s/acs/%s'
        self.definitions_url = 'https://api.census.gov/data/%s/acs/%s/variables.json'
        self.definition_url = 'https://api.census.gov/data/%s/acs/%s/variables/%s.json'
        self.groups_url = 'https://api.census.gov/data/%s/acs/%s/groups.json'

    dataset = 'acs5/subject'


class ACS1StClient(ACS5Client):
    def _switch_endpoints(self, year):
        self.endpoint_url = 'https://api.census.gov/data/%s/acs/%s'
        self.definitions_url = 'https://api.census.gov/data/%s/acs/%s/variables.json'
        self.definition_url = 'https://api.census.gov/data/%s/acs/%s/variables/%s.json'

    dataset = 'acs1/subject'

    years = (2023, 2022, 2021, 2020, 2019, 2018, 2017, 2016, 2015, 2014, 2013, 2012)


class ACS3Client(ACSClient):

    default_year = 2013
    dataset = 'acs3'

    years = (2013, 2012)

    @supported_years()
    def state_county_subdivision(self, fields, state_fips,
                                 county_fips, subdiv_fips, **kwargs):
        return self.get(fields, geo={
            'for': 'county subdivision:{}'.format(subdiv_fips),
            'in': 'state:{} county:{}'.format(state_fips, county_fips),
        }, **kwargs)


class ACS3DpClient(ACS3Client):

    dataset = 'acs3/profile'


class ACS1Client(ACSClient):

    default_year = 2023
    dataset = 'acs1'

    years = (2023, 2022, 2021, 2019, 2018, 2017, 2016, 2015, 2014, 2013, 2012, 2011)

    @supported_years()
    def state_county_subdivision(self, fields, state_fips,
                                 county_fips, subdiv_fips, **kwargs):
        return self.get(fields, geo={
            'for': 'county subdivision:{}'.format(subdiv_fips),
            'in': 'state:{} county:{}'.format(state_fips, county_fips),
        }, **kwargs)


class ACS1DpClient(ACS1Client):

    dataset = 'acs1/profile'

    years = (2023, 2022, 2021, 2019, 2018, 2017, 2016, 2015, 2014, 2013, 2012)


class SF1Client(Client):

    default_year = 2010
    dataset = 'sf1'

    years = (2010,)

    def _switch_endpoints(self, year):

        self.endpoint_url = 'https://api.census.gov/data/%s/dec/%s'
        self.definitions_url = 'https://api.census.gov/data/%s/dec/%s/variables.json'
        self.definition_url = 'https://api.census.gov/data/%s/dec/%s/variables/%s.json'
        self.groups_url = 'https://api.census.gov/data/%s/dec/%s/groups.json'

    def tables(self, *args, **kwargs):
        self._switch_endpoints(kwargs.get('year', self.default_year))
        return super(SF1Client, self).tables(*args, **kwargs)

    def get(self, *args, **kwargs):
        self._switch_endpoints(kwargs.get('year', self.default_year))

        return super(SF1Client, self).get(*args, **kwargs)

    @supported_years()
    def state_county_subdivision(self, fields, state_fips,
                                 county_fips, subdiv_fips, **kwargs):
        return self.get(fields, geo={
            'for': 'county subdivision:{}'.format(subdiv_fips),
            'in': 'state:{} county:{}'.format(state_fips, county_fips),
        }, **kwargs)

    @supported_years()
    def state_county_tract(self, fields, state_fips,
                           county_fips, tract, **kwargs):
        return self.get(fields, geo={
            'for': 'tract:{}'.format(tract),
            'in': 'state:{} county:{}'.format(state_fips, county_fips),
        }, **kwargs)

    @supported_years()
    def state_county_blockgroup(self, fields, state_fips, county_fips,
                                blockgroup, tract=None, **kwargs):
        geo = {
            'for': 'block group:{}'.format(blockgroup),
            'in': 'state:{} county:{}'.format(state_fips, county_fips),
        }
        if tract:
            geo['in'] += ' tract:{}'.format(tract)
        return self.get(fields, geo=geo, **kwargs)

    @supported_years(2010)
    def state_msa(self, fields, state_fips, msa, **kwargs):
        return self.get(fields, geo={
            'for': ('metropolitan statistical area/' +
                    'micropolitan statistical area (or part):{}'.format(msa)),
            'in': 'state:{}'.format(state_fips),
        }, **kwargs)

    @supported_years(2010)
    def state_csa(self, fields, state_fips, csa, **kwargs):
        return self.get(fields, geo={
            'for': 'combined statistical area (or part):{}'.format(csa),
            'in': 'state:{}'.format(state_fips),
        }, **kwargs)

    @supported_years(2010)
    def state_district_place(self, fields, state_fips,
                             district, place, **kwargs):
        return self.get(fields, geo={
            'for': 'place/remainder (or part):{}'.format(place),
            'in': 'state:{} congressional district:{}'.format(
                state_fips, district),
        }, **kwargs)

    @supported_years(2010)
    def state_zipcode(self, fields, state_fips, zcta, **kwargs):
        return self.get(fields, geo={
            'for': 'zip code tabulation area (or part):{}'.format(zcta),
            'in': 'state:{}'.format(state_fips),
        }, **kwargs)


class PLClient(Client):

    default_year = 2020
    dataset = 'pl'

    years = (2020, 2010, 2000)

    def _switch_endpoints(self, year):

        self.endpoint_url = 'https://api.census.gov/data/%s/dec/%s'
        self.definitions_url = 'https://api.census.gov/data/%s/dec/%s/variables.json'
        self.definition_url = 'https://api.census.gov/data/%s/dec/%s/variables/%s.json'
        self.groups_url = 'https://api.census.gov/data/%s/dec/%s/groups.json'

    def tables(self, *args, **kwargs):
        self._switch_endpoints(kwargs.get('year', self.default_year))
        return super(PLClient, self).tables(*args, **kwargs)

    def get(self, *args, **kwargs):
        self._switch_endpoints(kwargs.get('year', self.default_year))

        return super(PLClient, self).get(*args, **kwargs)

    @supported_years()
    def state_county_subdivision(self, fields, state_fips,
                                 county_fips, subdiv_fips, **kwargs):
        return self.get(fields, geo={
            'for': 'county subdivision:{}'.format(subdiv_fips),
            'in': 'state:{} county:{}'.format(state_fips, county_fips),
        }, **kwargs)

    @supported_years()
    def state_county_tract(self, fields, state_fips,
                           county_fips, tract, **kwargs):
        return self.get(fields, geo={
            'for': 'tract:{}'.format(tract),
            'in': 'state:{} county:{}'.format(state_fips, county_fips),
        }, **kwargs)

    @supported_years()
    def state_county_blockgroup(self, fields, state_fips, county_fips,
                                blockgroup, tract=None, **kwargs):
        geo = {
            'for': 'block group:{}'.format(blockgroup),
            'in': 'state:{} county:{}'.format(state_fips, county_fips),
        }
        if tract:
            geo['in'] += ' tract:{}'.format(tract)
        return self.get(fields, geo=geo, **kwargs)
    
    @supported_years()
    def state_elementary_school(self, fields, state_fips,
                           tract, **kwargs):
        return self.get(fields, geo={
            'for': 'school district (elementary):{}'.format(tract),
            'in': 'state:{}'.format(state_fips),
        }, **kwargs)


class Census(object):

    ALL = ALL

    def __init__(self, key, year=None, session=None):

        if not session:
            session = new_session()

        self.session = session
        self.session.headers.update({
            'User-Agent': ('python-census/{} '.format(__version__) +
                           'github.com/datamade/census')
        })

        self._acs = ACS5Client(key, year, session)  # deprecated
        self.acs5 = ACS5Client(key, year, session)
        self.acs3 = ACS3Client(key, year, session)
        self.acs1 = ACS1Client(key, year, session)
        self.acs5st = ACS5StClient(key, year, session)
        self.acs1st = ACS1StClient(key, year, session)
        self.acs5dp = ACS5DpClient(key, year, session)
        self.acs3dp = ACS3DpClient(key, year, session)
        self.acs1dp = ACS1DpClient(key, year, session)
        self.sf1 = SF1Client(key, year, session)
        self.pl = PLClient(key, year, session)

    @property
    def acs(self):
        warnings.warn('Use acs5 instead of acs', DeprecationWarning)
        return self._acs
