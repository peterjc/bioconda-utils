

import glob
import os
import re
import tempfile
import zipfile
import logging

import requests
import backoff
import json
from pathlib import Path
from bioconda_utils import utils
from bioconda_utils.upload import anaconda_upload, skopeo_upload

logger = logging.getLogger(__name__)


IMAGE_RE = re.compile(r"(.+)(?::|%3A)(.+)\.tar\.gz$")


def upload_pr_artifacts(config, repo, git_sha, dryrun=False, mulled_upload_target=None, label=None, artifact_source="azure") -> bool:
    _config = utils.load_config(config)
    repodata = utils.RepoData()

    gh = utils.get_github_client()

    repo = gh.get_repo(repo)

    commit = repo.get_commit(git_sha)
    prs = commit.get_pulls()
    if not prs:
        # no PR found for the commit
        return True
    pr = prs[0]
    artifacts = set(fetch_artifacts(pr, artifact_source))
    if not artifacts:
        # no artifacts found, fail and rebuild packages
        logger.info("No artifacts found.")
        return False
    else:
        for artifact in artifacts:
            with tempfile.TemporaryDirectory() as tmpdir:
                # download the artifact
                if artifact_source == "azure":
                    artifact_path = os.path.join(tmpdir, os.path.basename(artifact))
                    download_artifact(artifact, artifact_path)
                    zipfile.ZipFile(artifact_path).extractall(tmpdir)
                elif artifact_source == "circleci":
                    artifact_dir = os.path.join(tmpdir, *(artifact.split("/")[-4:-1]))
                    artifact_path = os.path.join(tmpdir, artifact_dir, os.path.basename(artifact))
                    Path(artifact_dir).mkdir(parents=True, exist_ok=True) 
                    download_artifact(artifact, artifact_path)

                # get all the contained packages and images and upload them
                platform_patterns = [repodata.platform2subdir(repodata.native_platform())]
                if repodata.native_platform().startswith("linux"):
                    platform_patterns.append("noarch")

                for platform_pattern in platform_patterns:
                    pattern = f"{tmpdir}/*/packages/{platform_pattern}/*.tar.bz2"
                    logger.info(f"Checking for packages at {pattern}.")
                    for pkg in glob.glob(pattern):
                        if dryrun:
                            logger.info(f"Would upload {pkg} to anaconda.org.")
                        else:
                            logger.info(f"Uploading {pkg} to anaconda.org.")
                            # upload the package
                            anaconda_upload(pkg, label=label)

                if mulled_upload_target:
                    quay_login = os.environ['QUAY_LOGIN']

                    pattern = f"{tmpdir}/*/images/*.tar.gz"
                    logger.info(f"Checking for images at {pattern}.")
                    for img in glob.glob(pattern):
                        m = IMAGE_RE.match(os.path.basename(img))
                        assert m, f"Could not parse image name from {img}"
                        name, tag = m.groups()
                        if label:
                            # add label to tag
                            tag = f"{tag}-{label}"
                        target = f"{mulled_upload_target}/{name}:{tag}"
                        # Skopeo can't handle a : in the file name
                        fixed_img_name = img.replace(":", "_")
                        os.rename(img, fixed_img_name)
                        if dryrun:
                            logger.info(f"Would upload {img} to {target}.")
                        else:
                            # upload the image
                            logger.info(f"Uploading {img} to {target}.")
                            skopeo_upload(fixed_img_name, target, creds=quay_login)
        return True


@backoff.on_exception(
    backoff.expo,
    requests.exceptions.RequestException
)
def download_artifact(url, to_path):
    logger.info(f"Downloading artifact {url}.")
    resp = requests.get(url, stream=True, allow_redirects=True)
    resp.raise_for_status()
    with open(to_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024):
            if chunk:
                f.write(chunk)


def fetch_artifacts(pr, artifact_source):
    """
    Fetch artifacts from a PR.

    Parameters
    ----------
    pr: PR number
    artifact_source: application hosting build artifacts (e.g., Azure or Circle CI)

    Returns
    -------
    """
    commits = pr.get_commits()
    # get the last commit
    commit = commits[commits.totalCount - 1]
    # get the artifacts
    check_runs = commit.get_check_runs()
    repodata = utils.RepoData()
    platform = repodata.native_platform()
    for check_run in check_runs:
        if (
            artifact_source == "azure" and 
            check_run.app.slug == "azure-pipelines" and
            check_run.name.startswith(f"bioconda.bioconda-recipes (test_{platform}")
        ):
            # azure builds
            artifact_url = get_azure_artifacts(check_run)
            yield from artifact_url
        elif (
            artifact_source == "circleci" and
            check_run.app.slug == "circleci-checks"
        ):
            # Circle CI builds
            artifact_url = get_circleci_artifacts(check_run, platform)
            yield from artifact_url


def get_azure_artifacts(check_run):
    azure_build_id = parse_azure_build_id(check_run.details_url)
    url = f"https://dev.azure.com/bioconda/bioconda-recipes/_apis/build/builds/{azure_build_id}/artifacts?api-version=4.1"
    res = requests.get(url, json=True).json()
    if res["count"] == 0:
        raise ValueError("No artifacts found!")
    else:
        for artifact in res["value"]:
            artifact_url = artifact["resource"]["downloadUrl"]
            yield artifact_url


def parse_azure_build_id(url: str) -> str:
    return re.search("buildId=(\d+)", url).group(1)


def get_circleci_artifacts(check_run, platform):
    circleci_workflow_id = json.loads(check_run.external_id)["workflow-id"]
    url_wf = f"https://circleci.com/api/v2/workflow/{circleci_workflow_id}/job"
    res_wf = requests.get(url_wf)
    json_wf = json.loads(res_wf.text)

    if len(json_wf["items"]) == 0:
        raise ValueError("No jobs found!")
    else:
        for job in json_wf["items"]:
            if job["name"].startswith(f"build_and_test-{platform}"):
                circleci_job_num = job["job_number"]
                url = f"https://circleci.com/api/v2/project/gh/bioconda/bioconda-recipes/{circleci_job_num}/artifacts"
                res = requests.get(url)
                json_job = json.loads(res.text)
                if len(json_job["items"]) == 0:
                    raise ValueError("No artifacts found!")
                else:
                    for artifact in json_job["items"]:
                        artifact_url = artifact["url"]
                        if artifact_url.endswith(".html") or artifact_url.endswith(".json") or artifact_url.endswith(".json.bz2"):
                            continue
                        else:
                            yield artifact_url
