"""Jenkins API client — job search and parameter discovery.

Used by JenkinsAction to:
  1. Fuzzy-match a user's free-text description to a real job name
  2. Fetch the job's defined parameters so the confirmation card shows them
"""
from __future__ import annotations

import difflib
from typing import Optional

import requests

from config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

_JOBS_CACHE: list[str] = []
_JOBS_CACHE_TS: float = 0.0
_JOBS_CACHE_TTL = 300  # 5 min


def _auth() -> tuple[str, str]:
    return (settings.JENKINS_USER, settings.JENKINS_API_TOKEN)


def list_jobs(force_refresh: bool = False) -> list[str]:
    """Return all top-level job names from Jenkins, cached for 5 min."""
    import time
    global _JOBS_CACHE, _JOBS_CACHE_TS

    if not force_refresh and _JOBS_CACHE and (time.time() - _JOBS_CACHE_TS) < _JOBS_CACHE_TTL:
        return _JOBS_CACHE

    if not settings.JENKINS_URL:
        return []

    try:
        url = f"{settings.JENKINS_URL.rstrip('/')}/api/json"
        resp = requests.get(
            url,
            params={"tree": "jobs[name]"},
            auth=_auth(),
            timeout=10,
        )
        resp.raise_for_status()
        jobs = [j["name"] for j in resp.json().get("jobs", [])]
        _JOBS_CACHE = jobs
        _JOBS_CACHE_TS = time.time()
        logger.info("Jenkins: fetched %d jobs", len(jobs))
        return jobs
    except Exception as exc:  # noqa: BLE001
        logger.warning("Jenkins job list failed: %s", exc)
        return _JOBS_CACHE  # return stale cache on error


def search_job(query: str) -> Optional[str]:
    """Find the best matching Jenkins job for a free-text query.

    Strategy:
      1. Exact match (case-insensitive)
      2. All query words appear in job name
      3. difflib closest match (cutoff 0.4)
    Returns None if no reasonable match found.
    """
    jobs = list_jobs()
    if not jobs:
        return None

    query_lower = query.lower().replace("-", " ").replace("_", " ")
    query_words = query_lower.split()

    # 1. Exact match
    for job in jobs:
        if job.lower() == query.lower():
            return job

    # 2. All words present
    candidates = [
        j for j in jobs
        if all(w in j.lower().replace("-", " ").replace("_", " ") for w in query_words)
    ]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        # Pick the shortest (most specific) match
        return min(candidates, key=len)

    # 3. difflib fuzzy match
    normalised = {j: j.lower().replace("-", " ").replace("_", " ") for j in jobs}
    matches = difflib.get_close_matches(query_lower, normalised.values(), n=1, cutoff=0.4)
    if matches:
        for job, norm in normalised.items():
            if norm == matches[0]:
                return job

    return None


def search_jobs(query: str, max_results: int = 5) -> list[str]:
    """Return multiple Jenkins jobs matching a free-text query, sorted by relevance.

    Unlike search_job() which returns only the single best match, this returns
    up to max_results candidates for passive browsing (no trigger intent).
    """
    jobs = list_jobs()
    if not jobs:
        return []

    query_lower = query.lower().replace("-", " ").replace("_", " ")
    query_words = [w for w in query_lower.split() if len(w) > 1]
    results: list[str] = []

    # 1. All query words present in job name
    for j in jobs:
        norm = j.lower().replace("-", " ").replace("_", " ")
        if all(w in norm for w in query_words):
            results.append(j)

    # 2. Partial word match — at least (N-1) words present
    if len(results) < max_results:
        threshold = max(1, len(query_words) - 1)
        normalised = {j: j.lower().replace("-", " ").replace("_", " ") for j in jobs}
        for job, norm in normalised.items():
            if job not in results:
                score = sum(1 for w in query_words if w in norm)
                if score >= threshold:
                    results.append(job)

    # 3. difflib fuzzy for remaining slots
    if len(results) < max_results:
        normalised = {j: j.lower().replace("-", " ").replace("_", " ") for j in jobs}
        fuzzy = difflib.get_close_matches(
            query_lower, normalised.values(),
            n=max_results - len(results), cutoff=0.35,
        )
        for match in fuzzy:
            for job, norm in normalised.items():
                if norm == match and job not in results:
                    results.append(job)

    return results[:max_results]


def get_job_params(job_name: str) -> list[dict]:
    """Fetch parameter definitions for a Jenkins job.

    Returns a list of dicts: [{name, default, description, type}, ...]
    type is the Jenkins parameter class short name, e.g.:
      'TextParameterDefinition'   — multi-line textarea
      'StringParameterDefinition' — single-line string
      'BooleanParameterDefinition', 'ChoiceParameterDefinition', etc.
    """
    if not settings.JENKINS_URL:
        return []

    try:
        url = f"{settings.JENKINS_URL.rstrip('/')}/job/{job_name}/api/json"
        resp = requests.get(
            url,
            params={"tree": "property[parameterDefinitions[name,type,defaultParameterValue[value],description]]"},
            auth=_auth(),
            timeout=10,
        )
        resp.raise_for_status()
        params = []
        for prop in resp.json().get("property", []):
            for p in prop.get("parameterDefinitions", []):
                params.append({
                    "name": p.get("name", ""),
                    "default": (p.get("defaultParameterValue") or {}).get("value", ""),
                    "description": p.get("description", ""),
                    "type": p.get("type", ""),
                })
        return params
    except Exception as exc:  # noqa: BLE001
        logger.warning("Jenkins param fetch failed for %s: %s", job_name, exc)
        return []
