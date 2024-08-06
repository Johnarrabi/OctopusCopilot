import asyncio
import datetime
import json
import re
from urllib.parse import urlparse

import aiohttp
import pytz
from fuzzywuzzy import fuzz
from retry import retry
from urllib3.exceptions import HTTPError

from domain.config.openai import max_context
from domain.converters.string_to_int import string_to_int
from domain.exceptions.request_failed import OctopusRequestFailed
from domain.exceptions.resource_not_found import ResourceNotFound
from domain.exceptions.runbook_not_published import RunbookNotPublished
from domain.exceptions.space_not_found import SpaceNotFound
from domain.exceptions.user_not_loggedin import OctopusApiKeyInvalid
from domain.logging.app_logging import configure_logging
from domain.query.query_inspector import release_is_latest
from domain.sanitizers.sanitized_list import get_item_fuzzy, normalize_log_step_name, flatten_list
from domain.sanitizers.url_sanitizer import quote_safe
from domain.url.build_url import build_url
from domain.validation.argument_validation import ensure_string_not_empty
from infrastructure.http_pool import http, TAKE_ALL

logger = configure_logging()
channel_cache = {}
tenant_cache = {}
environment_cache = {}

# Semaphore to limit the number of concurrent requests to GitHub
sem = asyncio.Semaphore(10)

# metadata prefix string, used with a regex match
metadata_prefix = "(\\s*(\\*|-)\\s*)?"


def logging_wrapper(func):
    def wrapper(*args, **kwargs):
        try:
            print(func.__name__ + " Enter")
            return func(*args, **kwargs)
        finally:
            print(func.__name__ + " Exit")

    return wrapper


def get_octopus_headers(my_api_key):
    """
    Build the headers used to make an Octopus API request
    :param my_api_key: The function used to get the Octopus API key
    :return: The headers required to call the Octopus API
    """

    if my_api_key is None:
        raise ValueError('my_api_key must be the Octopus API key.')

    return {
        "X-Octopus-ApiKey": my_api_key,
        "User-Agent": "OctopusAI",
    }


@logging_wrapper
def get_space_first_project_runbook_and_environment(space_id, api_key, url):
    """
    Attempt to return a sensible combination of environment, project, and runbook. This is often used to provide sample
    queries that are able to be run by copying and pasting. There is no guarantee that the three entities (project,
    runbook, and environment) are actually configured to use each other, just that the names are valid.
    :param space_id: The space ID
    :param api_key: The API key
    :param url: The Octopus URL
    :return: The first combination of project, runbook, and environment
    """
    space_first_runbook = next(get_all_runbooks_generator(space_id, api_key, url), None)
    space_first_project = next(get_projects_generator(space_id, api_key, url), None)
    space_first_environment = next(get_environments_generator(space_id, api_key, url), None)

    # If there was a runbook, return the runbook, the project it was associated with, and an environment
    if space_first_runbook:
        project = get_project(space_id, space_first_runbook["ProjectId"], api_key, url)
        return project, space_first_runbook, space_first_environment

    # Otherwise return the project and environment
    if space_first_project and space_first_environment:
        return space_first_project, None, space_first_environment

    # return nothing
    return None, None, None


@logging_wrapper
def get_space_id_and_name_from_name(space_name, my_api_key, my_octopus_api):
    """
    Gets a space ID and actual space name from a name extracted from a query.
    Note that we are quite lenient here in terms of whitespace and capitalisation.
    :param space_name: The name or id of the space
    :param my_octopus_api: The Octopus URL
    :param my_api_key: The Octopus API key
    :return: The space ID and actual name
    """

    ensure_string_not_empty(space_name, 'space_name must be a non-empty string (get_space_id_and_name_from_name).')
    ensure_string_not_empty(my_octopus_api,
                            'my_octopus_api must be the Octopus Url (get_space_id_and_name_from_name).')
    ensure_string_not_empty(my_api_key, 'my_api_key must be the Octopus Api key (get_space_id_and_name_from_name).')

    # Early exit if an ID was supplied
    if space_name.startswith("Spaces-"):
        space = get_space(space_name, my_api_key, my_octopus_api)
        return space["Id"], space["Name"]

    api = build_url(my_octopus_api, "api/spaces", dict(take=TAKE_ALL))
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(my_api_key)))
    json = resp.json()

    filtered_spaces = list(filter(lambda s: s["Name"] == space_name, json["Items"]))
    if len(filtered_spaces) == 1:
        return filtered_spaces[0]["Id"], filtered_spaces[0]["Name"]

    # try case-insensitive match and stripping and whitespace
    filtered_spaces = list(filter(lambda s: s["Name"].lower().strip() == space_name.lower().strip(), json["Items"]))
    if len(filtered_spaces) == 1:
        return filtered_spaces[0]["Id"], filtered_spaces[0]["Name"]

    raise SpaceNotFound(space_name)


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_version(octopus_url):
    api = build_url(octopus_url, "api")
    resp = handle_response(lambda: http.request("GET", api))
    return resp.json()["Version"]


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_spaces_batch(skip, take, api_key, octopus_url):
    api = build_url(octopus_url, "api/Spaces", dict(take=take, skip=skip))
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))
    return resp.json()["Items"]


@logging_wrapper
def get_spaces_generator(api_key, octopus_url):
    skip = 0
    take = 30

    while True:
        batch_spaces = get_spaces_batch(skip, take, api_key, octopus_url)

        for space in batch_spaces:
            yield space

        if len(batch_spaces) != take:
            break

        skip += take


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_octopus_project_names_base(space_name, my_api_key, my_octopus_api):
    """
    The base function used to get a list of project names.
    :param space_name: The name of the Octopus space containing the projects
    :param my_api_key: The Octopus API key
    :param my_octopus_api: The Octopus URL
    :return: The list of projects in the space
    """
    ensure_string_not_empty(space_name, 'space_name must be a non-empty string (get_octopus_project_names_base).')
    ensure_string_not_empty(my_octopus_api, 'my_octopus_api must be the Octopus Url (get_octopus_project_names_base).')
    ensure_string_not_empty(my_api_key, 'my_api_key must be the Octopus Api key (get_octopus_project_names_base).')

    space_id, actual_space_name = get_space_id_and_name_from_name(space_name, my_api_key, my_octopus_api)

    api = build_url(my_octopus_api, f"api/{quote_safe(space_id)}/Projects", dict(take=TAKE_ALL))
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(my_api_key)))

    json = resp.json()
    projects = list(map(lambda p: p["Name"], json["Items"]))

    return actual_space_name, projects


@logging_wrapper
def get_project_github_workflow(space_id, project_id, my_api_key, my_octopus_api):
    """
    Extracts the GitHub owner, repo, and workflow from the project description.
    :param space_id: The space id hosting the project
    :param project_id: The project ID
    :param my_api_key: The octopus API key
    :param my_octopus_api: The Octopus url
    :return: The owner, repo, and workflow ID (if found)
    """
    ensure_string_not_empty(space_id, 'space_id must not be empty (get_project_github_workflow).')
    ensure_string_not_empty(project_id, 'project_id must not be empty (get_project_github_workflow).')
    ensure_string_not_empty(my_api_key, 'my_api_key must not be empty (get_project_github_workflow).')
    ensure_string_not_empty(my_octopus_api, 'my_octopus_api must not be empty (get_project_github_workflow).')

    project = get_project(space_id, project_id, my_api_key, my_octopus_api)
    description = project["Description"].split("\n") if project["Description"] else []
    owner = next(
        map(
            lambda x: re.sub(f"{metadata_prefix}github owner:", "", x, flags=re.IGNORECASE).strip(),
            filter(lambda x: re.match(f"{metadata_prefix}github owner:", x, flags=re.IGNORECASE), description)),
        None)
    repo = next(
        map(
            lambda x: re.sub(f"{metadata_prefix}github repo:", "", x, flags=re.IGNORECASE).strip(),
            filter(lambda x: re.match(f"{metadata_prefix}github repo:", x, flags=re.IGNORECASE), description)),
        None)
    workflow = next(
        map(
            lambda x: re.sub(f"{metadata_prefix}github workflow:", "", x, flags=re.IGNORECASE).strip(),
            filter(lambda x: re.match(f"{metadata_prefix}github workflow:", x, flags=re.IGNORECASE), description)),
        None)

    if owner and repo and workflow:
        return {"ProjectId": project_id, "Owner": owner, "Repo": repo, "Workflow": workflow}

    return None


@logging_wrapper
async def get_release_github_workflow_async(space_id, release_id, my_api_key, my_octopus_api):
    """
    Extracts the GitHub owner, repo, and run id from the build information
    :param space_id: The space id hosting the project
    :param release_id: The release ID
    :param my_api_key: The octopus API key
    :param my_octopus_api: The Octopus url
    :return: The owner, repo, and workflow ID (if found)
    """
    release = await get_release_async(space_id, release_id, my_api_key, my_octopus_api)
    # First get runs from build information, and then fall back to release notes
    return get_release_github_workflow_from_buildinfo(release_id, release) or get_release_github_workflow_from_desc(
        release_id, release)


def get_release_github_workflow_from_buildinfo(release_id, release):
    # Get the build url and the package ID
    urls = filter(lambda x: x.get("BuildUrl"),
                  map(lambda x: {"BuildUrl": x.get("BuildUrl"), "PackageId": x.get("PackageId")},
                      release.get("BuildInformation", [])))
    # Keep the package ID and those with a build URL that matches the known github runs url
    workflows = filter(lambda x: x.get("Match"),
                       map(lambda x: {"PackageId": x.get("PackageId"),
                                      "Match": re.match(
                                          "/(?P<Owner>[^/]+)/(?P<Repo>[^/]+)/actions/runs/(?P<RunId>[^/]+)",
                                          urlparse(x.get("BuildUrl")).path)}, urls))
    # Extract all the useful values and return them in a map
    return list(
        map(lambda x: {"ReleaseId": release_id,
                       "PackageId": x.get("PackageId"),
                       "Owner": x["Match"].group("Owner"),
                       "Repo": x["Match"].group("Repo"),
                       "RunId": x["Match"].group("RunId")}, workflows))


def get_release_github_workflow_from_desc(release_id, release):
    description = release["ReleaseNotes"].split("\n") if release["ReleaseNotes"] else []
    owner = next(
        map(
            lambda x: re.sub(f"{metadata_prefix}github owner:", "", x, flags=re.IGNORECASE).strip(),
            filter(lambda x: re.match(f"{metadata_prefix}github owner:", x, flags=re.IGNORECASE), description)),
        None)
    repo = next(
        map(
            lambda x: re.sub(f"{metadata_prefix}?github repo:", "", x, flags=re.IGNORECASE).strip(),
            filter(lambda x: re.match(f"{metadata_prefix}github repo:", x, flags=re.IGNORECASE), description)),
        None)
    run_id = next(
        map(
            lambda x: re.sub(f"{metadata_prefix}github run\\s?id:", "", x, flags=re.IGNORECASE).strip(),
            filter(lambda x: re.match(f"{metadata_prefix}github run\\s?id:", x, flags=re.IGNORECASE), description)),
        None)

    if owner and repo and run_id:
        return [
            {"ReleaseId": release_id, "Owner": owner, "Repo": repo, "RunId": run_id}]

    return []


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_dashboard(space_id, my_api_key, my_octopus_api):
    """
    The base function used to get the dashboard summary
    :param space_id: The id of the Octopus space containing the projects
    :param my_api_key: The Octopus API key
    :param my_octopus_api: The Octopus URL
    :return: The actual space name and the dashboard summary
    """

    ensure_string_not_empty(space_id, 'space_id must be a non-empty string (get_dashboard).')
    ensure_string_not_empty(my_octopus_api, 'my_octopus_api must be the Octopus Url (get_dashboard).')
    ensure_string_not_empty(my_api_key, 'my_api_key must be the Octopus Api key (get_dashboard).')

    api = build_url(my_octopus_api, f"api/{quote_safe(space_id)}/Dashboard",
                    dict(highestLatestVersionPerProjectAndEnvironment="true"))
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(my_api_key)))

    return resp.json()


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_project_tenant_dashboard(space_id, project_id, my_api_key, my_octopus_api):
    """
    The base function used to get the tenanted dashboard for a project
    :param space_id: The id of the Octopus space containing the projects
    :param project_id: The id of the project
    :param my_api_key: The Octopus API key
    :param my_octopus_api: The Octopus URL
    :return: The project tenanted dashboard
    """

    ensure_string_not_empty(space_id, 'space_id must be a non-empty string (get_project_progression).')
    ensure_string_not_empty(space_id, 'project_id must be a non-empty string (get_project_progression).')
    ensure_string_not_empty(my_octopus_api, 'my_octopus_api must be the Octopus Url (get_project_progression).')
    ensure_string_not_empty(my_api_key, 'my_api_key must be the Octopus Api key (get_project_progression).')

    api = build_url(my_octopus_api,
                    f"bff/spaces/{quote_safe(space_id)}/Projects/{quote_safe(project_id)}/tenanted-dashboard",
                    dict(showAll="true", skip=0, take=30))
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(my_api_key)))

    return resp.json()


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_runbooks_dashboard(space_id, runbook_id, my_api_key, my_octopus_api):
    """
    The base function used to get the runbooks dashboard summary
    :param space_id: The id of the Octopus space containing the runbook
    :param runbook_id: The id of the runbook
    :param my_api_key: The Octopus API key
    :param my_octopus_api: The Octopus URL
    :return: The actual space name and the dashboard summary
    """

    ensure_string_not_empty(space_id, 'space_id must be a non-empty string (get_runbooks_dashboard).')
    ensure_string_not_empty(runbook_id, 'runbook_id must be a non-empty string (get_runbooks_dashboard).')
    ensure_string_not_empty(my_octopus_api, 'my_octopus_api must be the Octopus Url (get_runbooks_dashboard).')
    ensure_string_not_empty(my_api_key, 'my_api_key must be the Octopus Api key (get_runbooks_dashboard).')

    api = build_url(my_octopus_api, f"api/{quote_safe(space_id)}/progression/runbooks/{quote_safe(runbook_id)}")
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(my_api_key)))

    return resp.json()


@logging_wrapper
def get_current_user(my_api_key, my_octopus_api):
    """
    Returns the ID of the octopus user. This can be used to verify an API key, as even Octopus users with
    no permissions can access this endpoint.
    :param my_api_key: The Octopus API key
    :param my_octopus_api: The Octopus URL
    :return: The Octopus user ID
    """
    ensure_string_not_empty(my_octopus_api, 'my_octopus_api must be the Octopus Url (get_current_user).')
    ensure_string_not_empty(my_api_key, 'my_api_key must be the Octopus Api key (get_current_user).')

    api = build_url(my_octopus_api, "/api/users/me")
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(my_api_key)))

    json = resp.json()
    return json["Id"]


@logging_wrapper
@retry(HTTPError, tries=3, delay=2)
def get_projects(space_id, my_api_key, my_octopus_api):
    """
    Returns the projects in a space
    :param my_api_key: The Octopus API key
    :param my_octopus_api: The Octopus URL
    :return: The list of projects
    """
    ensure_string_not_empty(my_octopus_api, 'my_octopus_api must be the Octopus Url (get_projects).')
    ensure_string_not_empty(my_api_key, 'my_api_key must be the Octopus Api key (get_projects).')
    ensure_string_not_empty(space_id, 'space_id must be the space ID (get_projects).')

    api = build_url(my_octopus_api, f"/api/{quote_safe(space_id)}/Projects", query=dict(take=TAKE_ALL))
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(my_api_key)))

    json = resp.json()
    return json["Items"]


@logging_wrapper
def get_feeds(my_api_key, my_octopus_api, space_id):
    """
    Returns the feeds in a space
    :param my_api_key: The Octopus API key
    :param my_octopus_api: The Octopus URL
    :return: The list of feeds
    """
    ensure_string_not_empty(my_octopus_api, 'my_octopus_api must be the Octopus Url (get_feeds).')
    ensure_string_not_empty(my_api_key, 'my_api_key must be the Octopus Api key (get_feeds).')
    ensure_string_not_empty(space_id, 'space_id must be the space ID (get_feeds).')

    api = build_url(my_octopus_api, f"/api/{quote_safe(space_id)}/Feeds", query=dict(take=TAKE_ALL))
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(my_api_key)))

    json = resp.json()
    return json["Items"]


@logging_wrapper
def get_accounts(my_api_key, my_octopus_api, space_id):
    """
    Returns the accounts in a space
    :param my_api_key: The Octopus API key
    :param my_octopus_api: The Octopus URL
    :return: The list of accounts
    """
    ensure_string_not_empty(my_octopus_api, 'my_octopus_api must be the Octopus Url (get_accounts).')
    ensure_string_not_empty(my_api_key, 'my_api_key must be the Octopus Api key (get_accounts).')
    ensure_string_not_empty(space_id, 'space_id must be the space ID (get_accounts).')

    api = build_url(my_octopus_api, f"/api/{quote_safe(space_id)}/Accounts", query=dict(take=TAKE_ALL))
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(my_api_key)))

    json = resp.json()
    return json["Items"]


@logging_wrapper
def get_machines(my_api_key, my_octopus_api, space_id):
    """
    Returns the machines in a space
    :param my_api_key: The Octopus API key
    :param my_octopus_api: The Octopus URL
    :return: The list of machines
    """
    ensure_string_not_empty(my_octopus_api, 'my_octopus_api must be the Octopus Url (get_machines).')
    ensure_string_not_empty(my_api_key, 'my_api_key must be the Octopus Api key (get_machines).')
    ensure_string_not_empty(space_id, 'space_id must be the space ID (get_machines).')

    api = build_url(my_octopus_api, f"/api/{quote_safe(space_id)}/Machines", query=dict(take=TAKE_ALL))
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(my_api_key)))

    json = resp.json()
    return json["Items"]


@logging_wrapper
def get_certificates(my_api_key, my_octopus_api, space_id):
    """
    Returns the certificate in a space
    :param my_api_key: The Octopus API key
    :param my_octopus_api: The Octopus URL
    :return: The list of certificate
    """
    ensure_string_not_empty(my_octopus_api, 'my_octopus_api must be the Octopus Url (get_certificates).')
    ensure_string_not_empty(my_api_key, 'my_api_key must be the Octopus Api key (get_certificates).')
    ensure_string_not_empty(space_id, 'space_id must be the space ID (get_certificates).')

    api = build_url(my_octopus_api, f"/api/{quote_safe(space_id)}/Certificates", query=dict(take=TAKE_ALL))
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(my_api_key)))

    json = resp.json()
    return json["Items"]


@logging_wrapper
def get_environments(my_api_key, my_octopus_api, space_id):
    """
    Returns the environments in a space
    :param my_api_key: The Octopus API key
    :param my_octopus_api: The Octopus URL
    :return: The list of environments
    """
    ensure_string_not_empty(my_octopus_api, 'my_octopus_api must be the Octopus Url (get_environments).')
    ensure_string_not_empty(my_api_key, 'my_api_key must be the Octopus Api key (get_environments).')
    ensure_string_not_empty(space_id, 'space_id must be the space ID (get_environments).')

    api = build_url(my_octopus_api, f"/api/{quote_safe(space_id)}/Environments", query=dict(take=TAKE_ALL))
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(my_api_key)))

    json = resp.json()
    return json["Items"]


@logging_wrapper
def get_tenants(my_api_key, my_octopus_api, space_id):
    """
    Returns the environments in a space
    :param my_api_key: The Octopus API key
    :param my_octopus_api: The Octopus URL
    :return: The list of environments
    """
    ensure_string_not_empty(my_octopus_api, 'my_octopus_api must be the Octopus Url (get_environments).')
    ensure_string_not_empty(my_api_key, 'my_api_key must be the Octopus Api key (get_environments).')
    ensure_string_not_empty(space_id, 'space_id must be the space ID (get_environments).')

    api = build_url(my_octopus_api, f"/api/{quote_safe(space_id)}/Tenants", query=dict(take=TAKE_ALL))
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(my_api_key)))

    json = resp.json()
    return json["Items"]


@logging_wrapper
def get_project_channel(my_api_key, my_octopus_api, space_id, project_id):
    """
    Returns the channels associated with a project
    :param my_api_key: The Octopus API key
    :param my_octopus_api: The Octopus URL
    :return: The channels associated with the project
    """
    ensure_string_not_empty(my_octopus_api, 'my_octopus_api must be the Octopus Url (get_project_channel).')
    ensure_string_not_empty(my_api_key, 'my_api_key must be the Octopus Api key (get_project_channel).')
    ensure_string_not_empty(space_id, 'space_id must be the space ID (get_project_channel).')

    api = build_url(my_octopus_api, f"/api/{quote_safe(space_id)}/Projects/{quote_safe(project_id)}/Channels")
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(my_api_key)))

    json = resp.json()
    return json["Items"]


@logging_wrapper
def get_lifecycle(my_api_key, my_octopus_api, space_id, lifecycle_id):
    """
    Return the lifecycle with the given ID
    :param my_api_key: The Octopus API key
    :param my_octopus_api: The Octopus URL
    :return: The list of projects
    """
    ensure_string_not_empty(my_octopus_api, 'my_octopus_api must be the Octopus Url (get_lifecycle).')
    ensure_string_not_empty(my_api_key, 'my_api_key must be the Octopus Api key (get_lifecycle).')
    ensure_string_not_empty(space_id, 'space_id must be the space ID (get_lifecycle).')

    api = build_url(my_octopus_api, f"/api/{quote_safe(space_id)}/Lifecycles/{quote_safe(lifecycle_id)}")
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(my_api_key)))

    json = resp.json()
    return json


@logging_wrapper
def create_limited_api_key(user, my_api_key, my_octopus_api):
    """
    This function creates an API key that expires tomorrow.
    :param user: The current user
    :param my_api_key: The API key
    :param my_octopus_api: The Octopus URL
    :return:
    """

    ensure_string_not_empty(my_octopus_api, 'my_octopus_api must be the Octopus Url (create_limited_api_key).')
    ensure_string_not_empty(my_api_key, 'my_api_key must be the Octopus Api key (create_limited_api_key).')
    ensure_string_not_empty(user, 'user must be the Octopus user ID (create_limited_api_key).')

    tomorrow = datetime.datetime.now(pytz.UTC) + datetime.timedelta(days=1)

    api_key = {
        'Purpose': "Octopus Copilot temporary API key",
        'Expires': tomorrow.isoformat()
    }

    api = build_url(my_octopus_api, f"/api/users/{quote_safe(user)}/apikeys")
    resp = handle_response(lambda: http.request("POST", api, json=api_key, headers=get_octopus_headers(my_api_key)))

    json = resp.json()
    return json["ApiKey"]


@logging_wrapper
def create_unlimited_api_key(user, my_api_key, my_octopus_api):
    """
    This function creates an API key that does not expire.
    :param user: The current user
    :param my_api_key: The API key
    :param my_octopus_api: The Octopus URL
    :return:
    """

    ensure_string_not_empty(my_octopus_api, 'my_octopus_api must be the Octopus Url (create_limited_api_key).')
    ensure_string_not_empty(my_api_key, 'my_api_key must be the Octopus Api key (create_limited_api_key).')
    ensure_string_not_empty(user, 'user must be the Octopus user ID (create_limited_api_key).')

    tomorrow = datetime.datetime.now(pytz.UTC) + datetime.timedelta(days=1)

    api_key = {
        'Purpose': "Octopus Copilot temporary API key",
        'Expires': None
    }

    api = build_url(my_octopus_api, f"/api/users/{quote_safe(user)}/apikeys")
    resp = handle_response(lambda: http.request("POST", api, json=api_key, headers=get_octopus_headers(my_api_key)))

    json = resp.json()
    return json["ApiKey"]


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_raw_deployment_process(space_name, project_name, api_key, octopus_url):
    """
    Returns a deployment process as raw JSON.
    :param space_name: The name of the space.
    :param project_name: The name of the project
    :param api_key: The Octopus API key
    :param octopus_url: The Octopus URL
    :return: The deployment process raw JSON
    """
    ensure_string_not_empty(space_name, 'space_name must be a non-empty string (get_raw_deployment_process).')
    ensure_string_not_empty(project_name, 'project_name must be a non-empty string (get_raw_deployment_process).')

    space_id, actual_space_name = get_space_id_and_name_from_name(space_name, api_key, octopus_url)

    project = get_project(space_id, project_name, api_key, octopus_url)

    api = build_url(octopus_url, f"api/{quote_safe(space_id)}/Projects/{quote_safe(project['Id'])}/DeploymentProcesses")
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))

    return resp.data.decode("utf-8")


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_project_progression(space_name, project_name, api_key, octopus_url):
    """
    Returns a deployment progression for a project.
    :param space_name: The name of the space.
    :param project_name: The name of the project
    :param api_key: The Octopus API key
    :param octopus_url: The Octopus URL
    :return: The deployment progression raw JSON
    """
    ensure_string_not_empty(space_name, 'space_name must be a non-empty string (get_project_progression).')
    ensure_string_not_empty(project_name, 'project_name must be a non-empty string (get_project_progression).')

    space_id, actual_space_name = get_space_id_and_name_from_name(space_name, api_key, octopus_url)

    project = get_project(space_id, project_name, api_key, octopus_url)

    api = build_url(octopus_url, f"api/{quote_safe(space_id)}/Projects/{quote_safe(project['Id'])}/Progression")
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))

    return resp.json()


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_projects_batch(skip, take, space_id, api_key, octopus_url):
    api = build_url(octopus_url, f"api/{quote_safe(space_id)}/Projects", dict(take=take, skip=skip))
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))
    return resp.json()["Items"]


@logging_wrapper
def get_projects_generator(space_id, api_key, octopus_url):
    skip = 0
    take = 30

    while True:
        batch_projects = get_projects_batch(skip, take, space_id, api_key, octopus_url)

        for project in batch_projects:
            yield project

        if len(batch_projects) != take:
            break

        skip += take


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_environments_batch(skip, take, space_id, api_key, octopus_url):
    api = build_url(octopus_url, f"api/{quote_safe(space_id)}/Environments", dict(take=take, skip=skip))
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))
    return resp.json()["Items"]


@logging_wrapper
def get_environments_generator(space_id, api_key, octopus_url):
    skip = 0
    take = 30

    while True:
        batch_environments = get_environments_batch(skip, take, space_id, api_key, octopus_url)

        for environment in batch_environments:
            yield environment

        if len(batch_environments) != take:
            break

        skip += take


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_tenants_batch(skip, take, space_id, api_key, octopus_url):
    api = build_url(octopus_url, f"api/{quote_safe(space_id)}/Tenants", dict(take=take, skip=skip))
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))
    return resp.json()["Items"]


@logging_wrapper
def get_tenants_generator(space_id, api_key, octopus_url):
    skip = 0
    take = 30

    while True:
        batch_tenants = get_tenants_batch(skip, take, space_id, api_key, octopus_url)

        for tenant in batch_tenants:
            yield tenant

        if len(batch_tenants) != take:
            break

        skip += take


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_runbooks_batch(skip, take, space_id, project_id, api_key, octopus_url):
    api = build_url(octopus_url, f"api/{quote_safe(space_id)}/Projects/{quote_safe(project_id)}/Runbooks",
                    dict(take=take, skip=skip))
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))
    return resp.json()["Items"]


@logging_wrapper
def get_runbooks_generator(space_id, project_id, api_key, octopus_url):
    skip = 0
    take = 30

    while True:
        batch_runbooks = get_runbooks_batch(skip, take, space_id, project_id, api_key, octopus_url)

        for tenant in batch_runbooks:
            yield tenant

        if len(batch_runbooks) != take:
            break

        skip += take


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_all_runbooks_batch(skip, take, space_id, api_key, octopus_url):
    api = build_url(octopus_url, f"api/{quote_safe(space_id)}/Runbooks",
                    dict(take=take, skip=skip))
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))
    return resp.json()["Items"]


@logging_wrapper
def get_all_runbooks_generator(space_id, api_key, octopus_url):
    skip = 0
    take = 30

    while True:
        batch_runbooks = get_all_runbooks_batch(skip, take, space_id, api_key, octopus_url)

        for tenant in batch_runbooks:
            yield tenant

        if len(batch_runbooks) != take:
            break

        skip += take


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_space(space_id, api_key, octopus_url):
    """
    Returns a space resource from the id
    :param space_id: The ID of the space.
    :param api_key: The Octopus API key
    :param octopus_url: The Octopus URL
    :return: The space resource
    """
    ensure_string_not_empty(space_id, 'space_id must be a non-empty string (get_space).')
    ensure_string_not_empty(octopus_url,
                            'octopus_url must be the Octopus Url (get_space).')
    ensure_string_not_empty(api_key, 'api_key must be the Octopus Api key (get_space).')

    base_url = f"api/Spaces/{quote_safe(space_id)}"

    api = build_url(octopus_url, base_url)
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))
    return resp.json()


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_project(space_id, project_name, api_key, octopus_url):
    """
    Returns a project resource from the name
    :param space_id: The ID of the space.
    :param project_name: The name of the project
    :param api_key: The Octopus API key
    :param octopus_url: The Octopus URL
    :return: The project resource
    """
    ensure_string_not_empty(space_id, 'space_id must be a non-empty string (get_project).')
    ensure_string_not_empty(project_name, 'project_name must be a non-empty string (get_project).')

    # Early exit when a project ID is used
    if project_name.startswith("Projects-"):
        base_url = f"api/{quote_safe(space_id)}/Projects/{quote_safe(project_name)}"
        api = build_url(octopus_url, base_url)
        resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))
        return resp.json()

    base_url = f"api/{quote_safe(space_id)}/Projects"

    api = build_url(octopus_url, base_url, dict(partialname=project_name))
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))
    project = get_item_fuzzy(resp.json()["Items"], project_name)

    if project is None:
        api = build_url(octopus_url, base_url, dict(take=TAKE_ALL))
        resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))
        project = get_item_fuzzy(resp.json()["Items"], project_name)
        if project is None:
            raise ResourceNotFound("Project", project_name)

    return project


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_environment(space_id, environment_id, api_key, octopus_url):
    """
    Returns a environment resource from the id
    :param space_id: The ID of the space.
    :param environment_id: The Id of the environment
    :param api_key: The Octopus API key
    :param octopus_url: The Octopus URL
    :return: The environment resource
    """
    ensure_string_not_empty(space_id, 'space_id must be a non-empty string (get_environment).')
    ensure_string_not_empty(environment_id, 'environment_id must be a non-empty string (get_environment).')

    base_url = f"api/{quote_safe(space_id)}/Environments/{quote_safe(environment_id)}"

    api = build_url(octopus_url, base_url)
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))
    return resp.json()


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_project_releases(space_id, project_id, api_key, octopus_url, take=max_context):
    """
    Returns a deployment progression for a project.
    :param space_id: The ID of the space.
    :param project_id: The ID of the project
    :param api_key: The Octopus API key
    :param octopus_url: The Octopus URL
    :return: The deployment progression raw JSON
    """
    ensure_string_not_empty(space_id, 'space_id must be a non-empty string (get_project_releases).')
    ensure_string_not_empty(project_id, 'project_id must be a non-empty string (get_project_releases).')

    api = build_url(octopus_url, f"api/{quote_safe(space_id)}/Projects/{quote_safe(project_id)}/Releases",
                    query=dict(take=take))
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))

    return resp.json()


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_release_deployments(space_id, release_id, api_key, octopus_url):
    """
    Returns the deployments of a release.
    :param space_id: The ID of the space.
    :param release_id: The release ID
    :param api_key: The Octopus API key
    :param octopus_url: The Octopus URL
    :return: The deployment progression raw JSON
    """
    ensure_string_not_empty(space_id, 'space_id must be a non-empty string (get_release_deployments).')
    ensure_string_not_empty(release_id, 'release_id must be a non-empty string (get_release_deployments).')

    api = build_url(octopus_url, f"api/{quote_safe(space_id)}/Releases/{quote_safe(release_id)}/Deployments")
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))

    return resp.json()


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_release(space_id, release_id, api_key, octopus_url):
    """
    Returns the  release.
    :param space_id: The ID of the space.
    :param release_id: The release ID
    :param api_key: The Octopus API key
    :param octopus_url: The Octopus URL
    :return: The deployment progression raw JSON
    """
    ensure_string_not_empty(space_id, 'space_id must be a non-empty string (get_release_deployments).')
    ensure_string_not_empty(release_id, 'release_id must be a non-empty string (get_release_deployments).')

    api = build_url(octopus_url, f"api/{quote_safe(space_id)}/Releases/{quote_safe(release_id)}")
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))

    return resp.json()


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_task(space_id, task_id, api_key, octopus_url):
    """
    Returns the task.
    :param space_id: The ID of the space.
    :param task_id: The task ID
    :param api_key: The Octopus API key
    :param octopus_url: The Octopus URL
    :return: The deployment progression raw JSON
    """
    ensure_string_not_empty(space_id, 'space_id must be a non-empty string (get_task).')

    if not task_id:
        return None

    api = build_url(octopus_url, f"api/{quote_safe(space_id)}/Tasks/{quote_safe(task_id)}")
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))

    return resp.json()


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
async def get_task_details_async(space_id, task_id, api_key, octopus_url):
    """
    Returns the task.
    :param space_id: The ID of the space.
    :param task_id: The task ID
    :param api_key: The Octopus API key
    :param octopus_url: The Octopus URL
    :return: The deployment progression raw JSON
    """
    ensure_string_not_empty(space_id, 'space_id must be a non-empty string (get_task).')

    if not task_id:
        return None

    api = build_url(octopus_url, f"api/{quote_safe(space_id)}/Tasks/{quote_safe(task_id)}/details")

    async with sem:
        async with aiohttp.ClientSession(headers=get_octopus_headers(api_key)) as session:
            async with session.get(str(api)) as response:
                return await response.json()


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_project_progression_from_ids(space_id, project_id, api_key, octopus_url):
    """
    Returns a deployment progression for a project.
    :param space_id: The ID of the space.
    :param project_id: The ID of the project
    :param api_key: The Octopus API key
    :param octopus_url: The Octopus URL
    :return: The deployment progression raw JSON
    """
    ensure_string_not_empty(space_id, 'space_id must be a non-empty string (get_project_progression_from_ids).')
    ensure_string_not_empty(project_id, 'project_id must be a non-empty string (get_project_progression_from_ids).')

    api = build_url(octopus_url, f"api/{quote_safe(space_id)}/Projects/{quote_safe(project_id)}/Progression")
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))

    return resp.json()


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_deployment_status_base(space_name, environment_name, project_name, api_key, octopus_url):
    """
    The base function used to get a list of project names.
    :param space_name: The name of the Octopus space containing the projects
    :param project_name: The name of the Octopus project
    :param environment_name: The name of the Octopus environment
    :param api_key: The Octopus API key
    :param octopus_url: The Octopus URL
    :return: The list of projects in the space
    """

    logger.info("get_deployment_status - Enter")

    ensure_string_not_empty(space_name, 'space_name must be a non-empty string (get_deployment_status).')
    ensure_string_not_empty(project_name, 'project_name must be a non-empty string (get_deployment_status).')
    ensure_string_not_empty(environment_name,
                            'environment_name must be a non-empty string (get_deployment_status).')
    ensure_string_not_empty(octopus_url, 'octopus_url must be the Octopus Url (get_deployment_status).')
    ensure_string_not_empty(api_key, 'api_key must be the Octopus Api key (get_deployment_status).')

    space_id, actual_space_name = get_space_id_and_name_from_name(space_name, api_key, octopus_url)

    project = get_project(space_id, project_name, api_key, octopus_url)
    environment = get_environment_fuzzy(space_id, environment_name, api_key, octopus_url)

    api = build_url(octopus_url, f"api/{quote_safe(space_id)}/Projects/{quote_safe(project['Id'])}/Progression")
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))
    releases = list(filter(lambda r: environment["Id"] in r["Deployments"], resp.json()["Releases"]))

    if len(releases) == 0:
        raise ResourceNotFound("Deployment", f"{project_name} in {environment_name}")

    return actual_space_name, environment['Name'], project['Name'], releases[0]["Deployments"][environment['Id']][0]


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_deployment_logs(space_name, project_name, environment_name, tenant_name, release_version,
                        api_key,
                        octopus_url):
    """
    Returns a logs for a deployment to an environment.
    :param space_name: The name of the space.
    :param project_name: The name of the project
    :param environment_name: The name of the environment
    :param tenant_name: The name of the tenant
    :param release_version: The name of the release
    :param api_key: The Octopus API key
    :param octopus_url: The Octopus URL
    :return: The deployment progression raw JSON
    """
    ensure_string_not_empty(space_name, 'space_name must be a non-empty string (get_deployment_logs).')
    ensure_string_not_empty(project_name, 'project_name must be a non-empty string (get_deployment_logs).')
    ensure_string_not_empty(octopus_url, 'octopus_url must be the Octopus Url (get_deployment_logs).')
    ensure_string_not_empty(api_key, 'api_key must be the Octopus Api key (get_deployment_logs).')

    space_id, actual_space_name = get_space_id_and_name_from_name(space_name, api_key, octopus_url)

    project = get_project(space_id, project_name, api_key, octopus_url)

    environment = None
    if environment_name:
        environment = get_environment_fuzzy(space_id, environment_name, api_key, octopus_url)

    tenant = None
    if tenant_name:
        tenant = get_tenant_fuzzy(space_id, tenant_name, api_key, octopus_url)

    # Find deployment count
    # api = build_url(octopus_url, f"api/{quote_safe(space_id)}/Deployments", dict(take=0, projects=project['Id']))
    # resp_json = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key))).json()
    # total_results = resp_json["TotalResults"]
    # skip = max(0, total_results - 30)

    # Get the latest deployments
    api = build_url(octopus_url, f"api/{quote_safe(space_id)}/Deployments",
                    dict(take=100, skip=0, projects=project['Id']))
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))

    deployments = json.loads(resp.data.decode("utf-8")).get("Items")

    if environment:
        # Only releases to the environment are a candidate
        deployments = list(filter(lambda d: d["EnvironmentId"] == environment["Id"], deployments))

    if tenant:
        deployments = list(filter(lambda d: d["TenantId"] == tenant["Id"], deployments))

    task_id = None
    actual_release_version = None
    if release_is_latest(release_version):
        if deployments:
            task_id = deployments[0]["TaskId"]
            release = get_release(space_id, deployments[0]["ReleaseId"], api_key, octopus_url)
            actual_release_version = release["Version"]
    else:
        # We need to match the release version to a release, and the release to a deployment

        # Start by getting the releases for a project
        api = build_url(octopus_url, f"api/{quote_safe(space_id)}/Projects/{quote_safe(project['Id'])}/Releases",
                        dict(take=100))
        resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))
        releases = json.loads(resp.data.decode("utf-8")).get("Items")

        # Find the specific release
        release = next(filter(lambda r: r["Version"] == release_version.strip(), releases), None)

        # If the release is not found, exit
        if not release:
            return None, None

        # Find the specific deployment
        actual_release_version = release["Version"]
        specific_deployment = list(filter(lambda d: d["ReleaseId"] == release["Id"], deployments))
        if specific_deployment:
            task_id = specific_deployment[0]["TaskId"]

    if not task_id:
        return None, None

    api = build_url(octopus_url, f"api/{quote_safe(space_id)}/Tasks/{task_id}/details")
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))
    task = json.loads(resp.data.decode("utf-8"))

    return task["Task"], task["ActivityLogs"], actual_release_version


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_runbook_deployment_logs(space_name, project_name, runbook_name, environment_name, tenant_name, api_key,
                                octopus_url):
    """
    Returns a logs for a deployment to an environment.
    :param space_name: The name of the space.
    :param project_name: The name of the project
    :param runbook_name: The name of the runbook
    :param environment_name: The name of the environment
    :param tenant_name: The name of the tenant
    :param api_key: The Octopus API key
    :param octopus_url: The Octopus URL
    :return: The deployment progression raw JSON
    """
    ensure_string_not_empty(space_name, 'space_name must be a non-empty string (get_deployment_logs).')
    ensure_string_not_empty(project_name, 'project_name must be a non-empty string (get_deployment_logs).')
    ensure_string_not_empty(runbook_name, 'runbook_name must be a non-empty string (get_deployment_logs).')
    ensure_string_not_empty(octopus_url, 'octopus_url must be the Octopus Url (get_deployment_logs).')
    ensure_string_not_empty(api_key, 'api_key must be the Octopus Api key (get_deployment_logs).')

    space_id, actual_space_name = get_space_id_and_name_from_name(space_name, api_key, octopus_url)

    project = get_project(space_id, project_name, api_key, octopus_url)

    runbook = get_runbook_fuzzy(space_id, project["Id"], runbook_name, api_key, octopus_url)

    environment = None
    if environment_name:
        environment = get_environment_fuzzy(space_id, environment_name, api_key, octopus_url)

    tenant = None
    if tenant_name:
        tenant = get_tenant_fuzzy(space_id, tenant_name, api_key, octopus_url)

    # Find deployment count
    query = dict(skip=0, project=project['Id'], runbook=runbook["Id"], spaces=space_id,
                 includeSystem="false", environment=environment["Id"])

    if environment:
        query["environment"] = environment["Id"]

    if tenant:
        query["tenant"] = tenant["Id"]

    api = build_url(octopus_url, f"bff/tasks/list", query)
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))
    runs = json.loads(resp.data.decode("utf-8")).get("Items")

    if not runs:
        return ""

    task_id = runs[0]["Id"] if runs else None

    api = build_url(octopus_url, f"api/{quote_safe(space_id)}/Tasks/{task_id}/details")
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))
    task = json.loads(resp.data.decode("utf-8"))

    return task["ActivityLogs"]


def activity_logs_to_string(activity_logs, sanitized_steps=None, categories=None, join_string="\n", include_name=True):
    if not activity_logs:
        return ""

    logs = flatten_list(get_logs(i, 0, sanitized_steps, categories, include_name) for i in activity_logs)
    return join_string.join(logs)


def get_logs(log_item, depth, steps=None, categories=None, include_name=True):
    if depth == 0 and len(log_item["LogElements"]) == 0 and len(log_item["Children"]) == 0:
        return [f"No logs found (status: {log_item['Status']})."] if not categories else []

    filtered_logs = filter(lambda x: x["Category"] in categories, log_item["LogElements"]) if categories else log_item[
        "LogElements"]

    logs = []

    if include_name:
        logs.append(log_item["Name"])

    logs.extend(list(map(lambda e: e["MessageText"], filtered_logs)))

    # limit the result to either step indexes or names
    if depth == 1 and not filter_logs(log_item, steps):
        return logs

    if log_item["Children"]:
        for child in log_item["Children"]:
            logs.extend(get_logs(child, depth + 1, steps, categories, include_name))

    return logs


def filter_logs(log_item, steps):
    """
    Determines if the current step should be included in the logs
    :param log_item: The current log item to be serialized to a string
    :param steps: The list of steps to filter the logs by
    :return: True if the current step should be included in the logs, False otherwise
    """
    step_name_match_ratio = 80

    if not steps or len(steps) == 0:
        return True

    step_ints = [step for step in steps if string_to_int(step)]
    # Find the logs by index
    found_index = step_ints and len(step_ints) != 0 and any(
        filter(lambda step_int: log_item["Name"].startswith("Step " + step_int), step_ints))

    # Find the logs by name
    found_name = any(filter(lambda step: fuzz.ratio(normalize_log_step_name(step),
                                                    normalize_log_step_name(log_item["Name"])) >= step_name_match_ratio,
                            steps))

    # If none match, don't dig deeper
    if not found_index and not found_name:
        return False

    return True


def handle_response(callback):
    """
    This function maps common HTTP response codes to exceptions
    :param callback: A function that returns a response object
    :return: The response object
    """
    response = callback()
    if response.status == 401:
        logger.info(response.data.decode('utf-8'))
        raise OctopusApiKeyInvalid()
    if response.status != 200 and response.status != 201:
        logger.info(response.data.decode('utf-8'))
        raise OctopusRequestFailed(f"Request failed with " + response.data.decode('utf-8'))

    return response


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_environment_fuzzy(space_id, environment_name, api_key, octopus_url):
    base_url = f"api/{quote_safe(space_id)}/Environments"
    api = build_url(octopus_url, base_url, dict(partialname=environment_name))
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))
    environment = get_item_fuzzy(resp.json()["Items"], environment_name)

    if environment is None:
        api = build_url(octopus_url, base_url)
        resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))
        environment = get_item_fuzzy(resp.json()["Items"], environment_name)
        if environment is None:
            raise ResourceNotFound("Environment", environment_name)

    return environment


@logging_wrapper
def get_environments_fuzzy_cached(space_id, environment_names, api_key, octopus_url):
    if not environment_names:
        return []

    return list(map(lambda env: get_environment_fuzzy_cached(space_id, env, api_key, octopus_url),
                    environment_names))


@logging_wrapper
def get_environment_fuzzy_cached(space_id, environment_name, api_key, octopus_url):
    if not environment_cache.get(octopus_url):
        environment_cache[octopus_url] = {}

    if not environment_cache[octopus_url].get(space_id):
        environment_cache[octopus_url][space_id] = {}

    if not environment_cache[octopus_url][space_id].get(environment_name):
        environment_cache[octopus_url][space_id][environment_name] = get_environment_fuzzy(space_id, environment_name,
                                                                                           api_key, octopus_url)

    return environment_cache[octopus_url][space_id][environment_name]


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_tenant_fuzzy(space_id, tenant_name, api_key, octopus_url):
    base_url = f"api/{quote_safe(space_id)}/Tenants"
    api = build_url(octopus_url, base_url, dict(partialname=tenant_name))
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))
    tenant = get_item_fuzzy(resp.json()["Items"], tenant_name)

    if tenant is None:
        api = build_url(octopus_url, base_url)
        resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))
        tenant = get_item_fuzzy(resp.json()["Items"], tenant_name)
        if tenant is None:
            raise ResourceNotFound("Tenant", tenant_name)

    return tenant


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_tenant(space_id, tenant_id, api_key, octopus_url):
    base_url = f"api/{quote_safe(space_id)}/Tenants/{quote_safe(tenant_id)}"
    api = build_url(octopus_url, base_url)
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))
    return resp.json()


@logging_wrapper
def get_tenants_fuzzy_cached(space_id, tenant_names, api_key, octopus_url):
    return list(
        map(lambda tenant: get_tenant_fuzzy_cached(space_id, tenant, api_key, octopus_url),
            tenant_names)) if tenant_names else []


@logging_wrapper
def get_tenant_fuzzy_cached(space_id, tenant_name, api_key, octopus_url):
    if not tenant_cache.get(octopus_url):
        tenant_cache[octopus_url] = {}

    if not tenant_cache[octopus_url].get(space_id):
        tenant_cache[octopus_url][space_id] = {}

    if not tenant_cache[octopus_url][space_id].get(tenant_name):
        tenant_cache[octopus_url][space_id][tenant_name] = get_tenant_fuzzy(space_id, tenant_name, api_key, octopus_url)

    return tenant_cache[octopus_url][space_id][tenant_name]


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_channels(space_id, project_id, api_key, octopus_url):
    api = build_url(octopus_url, f"api/{quote_safe(space_id)}/projects/{quote_safe(project_id)}/Channels")
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))
    channels = resp.json()
    return channels['Items']


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_channel(space_id, channel_id, api_key, octopus_url):
    api = build_url(octopus_url, f"api/{quote_safe(space_id)}/Channels/{quote_safe(channel_id)}")
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))

    return resp.json()


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_channel_by_name(space_id, project_id, channel_name, api_key, octopus_url):
    channels = get_channels(space_id, project_id, api_key, octopus_url)
    matching_channel = get_item_fuzzy(channels, channel_name)
    if matching_channel is None:
        raise ResourceNotFound("Channel", project_id)

    return matching_channel


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_default_channel(space_id, project_id, api_key, octopus_url):
    channels = get_channels(space_id, project_id, api_key, octopus_url)
    default_channel = [channel for channel in channels if channel['IsDefault']]
    if len(default_channel) == 0:
        raise ResourceNotFound("Default Channel", project_id)

    return default_channel[0]


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_release_fuzzy(space_id, project_id, release_version, api_key, octopus_url):
    base_url = f"api/{quote_safe(space_id)}/projects/{quote_safe(project_id)}/releases"
    api = build_url(octopus_url, base_url, dict(searchByVersion=release_version))
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))
    releases = resp.json()
    matching_releases = [release for release in releases['Items'] if release['Version'] == release_version]
    if len(matching_releases) == 0:
        raise ResourceNotFound("Release", release_version)

    return matching_releases[0]


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_version_controlled_project_release_template(space_id, project_id, channel_id, git_ref, api_key, octopus_url):
    api = build_url(octopus_url,
                    f"api/{quote_safe(space_id)}/projects/{quote_safe(project_id)}/{quote_safe(git_ref)}/deploymentprocesses/template?channel={quote_safe(channel_id)}")
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))
    return resp.json()


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_database_project_release_template(space_id, project_id, channel_id, api_key, octopus_url):
    api = build_url(octopus_url,
                    f"api/{quote_safe(space_id)}/deploymentprocesses/deploymentprocess-{quote_safe(project_id)}/template?channel={quote_safe(channel_id)}")
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))
    return resp.json()


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_release_template_and_default_branch(space_id, project, channel_id, git_ref, api_key,
                                            octopus_url):
    default_branch = None
    if project['IsVersionControlled']:
        if not git_ref:
            default_branch_name = project['PersistenceSettings']['DefaultBranch']
            default_branch = get_project_version_controlled_branch(space_id, project['Id'], default_branch_name,
                                                                   api_key, octopus_url)
            # Assign default_branch to both variables.
            default_branch = git_ref = default_branch['CanonicalName']

        release_template = get_version_controlled_project_release_template(space_id, project['Id'],
                                                                           channel_id, git_ref,
                                                                           api_key, octopus_url)
    else:
        release_template = get_database_project_release_template(space_id, project['Id'], channel_id, api_key,
                                                                 octopus_url)
    return release_template, default_branch


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_project_version_controlled_branch(space_id, project_id, branch_name, api_key, octopus_url):
    api = build_url(octopus_url,
                    f"api/{quote_safe(space_id)}/projects/{quote_safe(project_id)}/git/branches/{quote_safe(branch_name)}")
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))
    return resp.json()


@logging_wrapper
def get_channel_cached(space_id, channel_id, api_key, octopus_url):
    if not channel_cache.get(octopus_url):
        channel_cache[octopus_url] = {}

    if not channel_cache[octopus_url].get(space_id):
        channel_cache[octopus_url][space_id] = {}

    if not channel_cache[octopus_url][space_id].get(channel_id):
        channel_cache[octopus_url][space_id][channel_id] = get_channel(space_id, channel_id, api_key, octopus_url)

    return channel_cache[octopus_url][space_id][channel_id]


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_packages(space_id, feed_id, package_id, api_key, octopus_url, take=1):
    base_url = f'api/{quote_safe(space_id)}/feeds/{quote_safe(feed_id)}/packages/versions'
    api = build_url(octopus_url, base_url, dict(take=take, packageId=quote_safe(package_id)))
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))
    json = resp.json()
    return json["Items"]


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_project_fuzzy(space_id, project_name, api_key, octopus_url):
    # First try to find a nice match using a partial name lookup.
    # This is a shortcut that means we don't have to loop the entire list of project.
    # This will succeed if any resources match the supplied partial name.
    base_url = f"api/{quote_safe(space_id)}/Projects"
    api = build_url(octopus_url, base_url, dict(partialname=project_name))
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))
    project = get_item_fuzzy(resp.json()["Items"], project_name)

    # This is a higher cost fallback used when the partial name returns no results.
    if project is None:
        api = build_url(octopus_url, base_url)
        resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))
        project = get_item_fuzzy(resp.json()["Items"], project_name)
        if project is None:
            raise ResourceNotFound("Project", project_name)

    return project


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_runbook_fuzzy(space_id, project_id, runbook_name, api_key, octopus_url):
    # First try to find a nice match using a partial name lookup.
    # This is a shortcut that means we don't have to loop the entire list of runbooks.
    # This will succeed if any resources match the supplied partial name.
    base_url = f"api/{quote_safe(space_id)}/Projects/{quote_safe(project_id)}/Runbooks"
    api = build_url(octopus_url, base_url, dict(partialname=runbook_name))
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))
    runbook = get_item_fuzzy(resp.json()["Items"], runbook_name)

    # This is a higher cost fallback used when the partial name returns no results.
    if runbook is None:
        api = build_url(octopus_url, base_url)
        resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))
        runbook = get_item_fuzzy(resp.json()["Items"], runbook_name)
        if runbook is None:
            raise ResourceNotFound("Runbook", runbook_name)

    return runbook


@logging_wrapper
def run_published_runbook_fuzzy(space_id, project_name, runbook_name, environment_name, tenant_name, my_api_key,
                                my_octopus_api, log_query=None):
    """
    Runs a published runbook
    """
    ensure_string_not_empty(my_octopus_api, 'my_octopus_api must be the Octopus Url (run_published_runbook_fuzzy).')
    ensure_string_not_empty(my_api_key, 'my_api_key must be the Octopus Api key (run_published_runbook_fuzzy).')
    ensure_string_not_empty(space_id, 'space_id must be the space ID (run_published_runbook_fuzzy).')
    ensure_string_not_empty(project_name, 'project_name must be the project (run_published_runbook_fuzzy).')
    ensure_string_not_empty(runbook_name, 'runbook_name must be the runbook (run_published_runbook_fuzzy).')
    ensure_string_not_empty(environment_name, 'environment_name must be the environment (run_published_runbook_fuzzy).')

    project = get_project_fuzzy(space_id, project_name, my_api_key, my_octopus_api)
    runbook = get_runbook_fuzzy(space_id, project["Id"], runbook_name, my_api_key, my_octopus_api)
    environment = get_environment_fuzzy(space_id, environment_name, my_api_key, my_octopus_api)
    tenant = get_tenant_fuzzy(space_id, tenant_name, my_api_key, my_octopus_api) if tenant_name else None

    if not runbook['PublishedRunbookSnapshotId']:
        raise RunbookNotPublished(runbook_name)

    base_url = f"api/{quote_safe(space_id)}/runbookRuns"
    api = build_url(my_octopus_api, base_url)

    runbook_run = {
        'RunbookId': runbook['Id'],
        'RunbookSnapshotId': runbook['PublishedRunbookSnapshotId'],
        'EnvironmentId': environment['Id'],
        'TenantId': tenant['Id'] if tenant else None,
        'SkipActions': None,
        'SpecificMachineIds': None,
        'ExcludedMachineIds': None
    }

    if log_query:
        log_query("run_published_runbook_fuzzy", f"""
                    Space: {space_id}
                    Project Names: {project_name}
                    Project Id: {project['Id']}
                    Runbook Names: {runbook_name}
                    Runbook Id: {runbook['Id']}
                    Runbook Published Snapshot Id: {runbook['PublishedRunbookSnapshotId']}
                    Tenant Names: {tenant_name}
                    Tenant Id: {tenant['Id'] if tenant else None}
                    Environment Names: {environment_name}
                    Environment Id: {environment['Id']}""")

    response = handle_response(
        lambda: http.request("POST", api, json=runbook_run, headers=get_octopus_headers(my_api_key)))

    return response.json()


@logging_wrapper
def create_release_fuzzy(space_id, project_name, git_ref, release_version, channel_name, my_api_key,
                         my_octopus_api, log_query=None):
    """
    Creates a release
    """
    ensure_string_not_empty(my_octopus_api, 'my_octopus_api must be the Octopus Url (create_release_fuzzy).')
    ensure_string_not_empty(my_api_key, 'my_api_key must be the Octopus Api key (create_release_fuzzy).')
    ensure_string_not_empty(space_id, 'space_id must be the space ID (create_release_fuzzy).')
    ensure_string_not_empty(project_name, 'project_name must be the project (create_release_fuzzy).')
    ensure_string_not_empty(release_version, 'release_version must be the release version (create_release_fuzzy).')

    project = get_project_fuzzy(space_id, project_name, my_api_key, my_octopus_api)

    base_url = f"api/{quote_safe(space_id)}/releases"
    api = build_url(my_octopus_api, base_url)

    if not channel_name:
        channel = get_default_channel(space_id, project['Id'], my_api_key, my_octopus_api)
    else:
        channel = get_channel_by_name(space_id, project['Id'], channel_name, my_api_key, my_octopus_api)

    release_template, default_branch = get_release_template_and_default_branch(space_id, project, channel['Id'],
                                                                               git_ref,
                                                                               my_api_key,
                                                                               my_octopus_api)
    if not git_ref:
        git_ref = default_branch

    release_request = {
        'ChannelId': channel['Id'],
        'ProjectId': project['Id'],
        'Version': release_version,
        'VersionControlReference': {},
        'SelectedPackages': []
    }

    if project['IsVersionControlled']:
        release_request['VersionControlReference']['GitRef'] = git_ref

    # Get default package versions
    for template_package in release_template['Packages']:
        packages = get_packages(space_id, template_package['FeedId'], template_package['PackageId'], my_api_key,
                                my_octopus_api)
        selected_package = {
            'ActionName': template_package['ActionName'],
            'PackageReferenceName': template_package['PackageReferenceName'],
            'Version': packages[0]['Version']
        }
        release_request['SelectedPackages'].append(selected_package)

    if log_query:
        log_query("create_release_fuzzy", f"""
                    Space: {space_id}
                    Project Names: {project_name}
                    Project Id: {project['Id']}
                    GitRef: {git_ref}
                    Channel Id: {channel['Id']}
                    Version: {release_version}
                    Selected Packages: {",".join(map(lambda p: f"{p['ActionName']}:{p['Version']}" + (f" ({p['PackageReferenceName']})" if p['PackageReferenceName'] else ""), release_request['SelectedPackages']))}""")

    response = handle_response(
        lambda: http.request("POST", api, json=release_request, headers=get_octopus_headers(my_api_key)))

    return response.json()


@logging_wrapper
def deploy_release_fuzzy(space_id, project_id, release_id, environment_name, tenant_name,
                         my_api_key, my_octopus_api, log_query=None):
    """
    Deploys a release
    """
    ensure_string_not_empty(my_octopus_api, 'my_octopus_api must be the Octopus Url (deploy_release_fuzzy).')
    ensure_string_not_empty(my_api_key, 'my_api_key must be the Octopus Api key (deploy_release_fuzzy).')
    ensure_string_not_empty(space_id, 'space_id must be the space ID (deploy_release_fuzzy).')
    ensure_string_not_empty(project_id, 'project_id must be the project ID (deploy_release_fuzzy).')
    ensure_string_not_empty(release_id, 'release_id must be the release ID (deploy_release_fuzzy).')
    ensure_string_not_empty(environment_name, 'environment_name must be the environment (deploy_release_fuzzy).')

    base_url = f"api/{quote_safe(space_id)}/deployments"
    api = build_url(my_octopus_api, base_url)

    # Get environment
    environment = get_environment_fuzzy(space_id, environment_name, my_api_key, my_octopus_api)
    environment_id = environment['Id']

    # Get tenant
    tenant = None
    if tenant_name:
        tenant = get_tenant_fuzzy(space_id, tenant_name, my_api_key, my_octopus_api)

    deploy_request = {
        'EnvironmentId': environment_id,
        'ProjectId': project_id,
        'ReleaseId': release_id,
        'TenantId': tenant['Id'] if tenant_name else None,
        'Priority': 'LifecycleDefault'
    }

    if log_query:
        log_query("deploy_release_fuzzy", f"""
                    Space: {space_id}
                    Project Id: {project_id}
                    Release Id: {release_id}
                    Environment Id: {environment_id}
                    Tenant ID: {tenant['Id'] if tenant_name else None}""")

    response = handle_response(
        lambda: http.request("POST", api, json=deploy_request, headers=get_octopus_headers(my_api_key)))

    return response.json()


async def get_release_async(space_id, release_id, api_key, octopus_url):
    ensure_string_not_empty(space_id, 'space_id must be a non-empty string (get_release_async).')
    ensure_string_not_empty(release_id, 'release_id must be a non-empty string (get_release_async).')
    ensure_string_not_empty(api_key, 'api_key must be a non-empty string (get_release_async).')
    ensure_string_not_empty(octopus_url, 'octopus_url must be a non-empty string (get_release_async).')

    api = build_url(octopus_url, f"api/{quote_safe(space_id)}/Releases/{quote_safe(release_id)}")

    async with sem:
        async with aiohttp.ClientSession(headers=get_octopus_headers(api_key)) as session:
            async with session.get(str(api)) as response:
                return await response.json()


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_artifacts(space_id, server_task, api_key, octopus_url):
    """
    Example JSON response:

    {
      "ItemType": "Artifact",
      "TotalResults": 1,
      "ItemsPerPage": 2147483647,
      "NumberOfPages": 1,
      "LastPageNumber": 0,
      "Items": [
        {
          "Id": "Artifacts-20909",
          "SpaceId": "Spaces-2328",
          "Filename": "depscan-bom.json",
          "Source": null,
          "ServerTaskId": "ServerTasks-1026286",
          "Created": "2024-07-01T01:34:25.016+00:00",
          "LogCorrelationId": "ServerTasks-1026286_DE6PC89RYP/260eebeeae6e4a499a31e72e2a946c32/6625ae643de84ee696546178ced3ecaa",
          "Links": {
            "Self": "/api/Spaces-2328/artifacts/Artifacts-20909",
            "Content": "/api/Spaces-2328/artifacts/Artifacts-20909/content"
          }
        }
      ],
      "Links": {
        "Self": "/api/Spaces-2328/artifacts?skip=0&take=2147483647&regarding=ServerTasks-1026286&order=asc",
        "Template": "/api/Spaces-2328/artifacts{?skip,take,regarding,ids,partialName,order}",
        "Page.All": "/api/Spaces-2328/artifacts?skip=0&take=2147483647&regarding=ServerTasks-1026286&order=asc",
        "Page.Current": "/api/Spaces-2328/artifacts?skip=0&take=2147483647&regarding=ServerTasks-1026286&order=asc",
        "Page.Last": "/api/Spaces-2328/artifacts?skip=0&take=2147483647&regarding=ServerTasks-1026286&order=asc"
      }
    }

    :param space_id: The space ID
    :param server_task: The server task ID
    :param api_key: The Octopus API key
    :param octopus_url: The Octopus server URL
    :return:
    """
    base_url = f"api/{quote_safe(space_id)}/artifacts"
    api = build_url(octopus_url, base_url, dict(regarding=server_task, take=TAKE_ALL))
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))

    return resp.json()


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_task_interruptions(space_id, server_task, api_key, octopus_url):
    """

    Get interruptions for a task
    :param space_id: The space ID
    :param server_task: The server task ID
    :param api_key: The Octopus API key
    :param octopus_url: The Octopus server URL
    :return: The interruptions for an Octopus Server task
    """

    base_url = f"api/{quote_safe(space_id)}/interruptions"
    api = build_url(octopus_url, base_url, dict(regarding=server_task))
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))
    interruptions = resp.json()
    if len(interruptions['Items']) == 0:
        return None

    return interruptions['Items']


@retry(HTTPError, tries=3, delay=2)
@logging_wrapper
def get_users(api_key, octopus_url):
    """
    Get the list of users
    :param api_key: The Octopus API key
    :param octopus_url: The Octopus server URL
    :return: The users for an Octopus Server task
    """

    base_url = f"api/users"
    api = build_url(octopus_url, base_url, dict(take=TAKE_ALL))
    resp = handle_response(lambda: http.request("GET", api, headers=get_octopus_headers(api_key)))
    interruptions = resp.json()
    if len(interruptions['Items']) == 0:
        return None

    return interruptions['Items']
