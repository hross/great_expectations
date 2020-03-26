import pytest

import copy
import subprocess
import time
import signal
import datetime
import requests
import boto3

from great_expectations.data_context.util import file_relative_path

USAGE_STATISTICS_QA_URL = "https://qa.stats.greatexpectations.io/great_expectations/v1/usage_statistics"

logGroupName = "/great_expectations/usage_statistics/qa"


@pytest.fixture(scope="session")
def valid_usage_statistics_message():
    return {
        "event_time": "2020-01-01T05:02:00.012Z",
        "data_context_id": "51ff737e-33af-455a-8a11-0dc923dcbfb5",
        "data_context_instance_id": "2d24776c-abef-4521-b182-4b18375b7259",
        "ge_version": "0.9.4",
        "method": "data_context.__init__",
        "success": True,
        "event_payload": {
            "platform.system": "Darwin",
            "platform.release": "19.3",
            "version_info": "sys.version_info(major=3, minor=7, micro=5, releaselevel='final', serial=0)",
            "anonymized_datasources": [
                {
                    "parent_class": "PandasDatasource"
                }
            ]
        }
    }


@pytest.fixture(scope="session")
def logstream(valid_usage_statistics_message):
    client = boto3.client('logs', region_name='us-east-1')
    # Warm up a logstream
    logStreamName = None
    message = copy.deepcopy(valid_usage_statistics_message)
    message["method"] = "logstream.__warmup__"
    requests.post(USAGE_STATISTICS_QA_URL, json=message)
    attempts = 0
    while attempts < 3:
        attempts += 1
        time.sleep(2)
        logStreams = client.describe_log_streams(
            logGroupName=logGroupName,
            orderBy='LastEventTime'
        )
        lastEventTimestamp = logStreams["logStreams"][-1].get("lastEventTimestamp")
        if lastEventTimestamp is not None:
            lastEvent = datetime.datetime.fromtimestamp(lastEventTimestamp / 1000)
            if (lastEvent - datetime.datetime.now()) < datetime.timedelta(seconds=30):
                logStreamName = logStreams["logStreams"][-1]["logStreamName"]
                break
    if logStreamName is None:
        assert False, "Unable to warm up a log stream for integration testing."
    return client, logStreamName


def test_send_malformed_data(valid_usage_statistics_message):
    # We should be able to successfully send a valid message, but find that
    # a malformed message is not accepted
    res = requests.post(USAGE_STATISTICS_QA_URL, json=valid_usage_statistics_message)
    assert res.status_code == 201
    invalid_usage_statistics_message = copy.deepcopy(valid_usage_statistics_message)
    del invalid_usage_statistics_message["data_context_id"]
    res = requests.post(USAGE_STATISTICS_QA_URL, json=invalid_usage_statistics_message)
    assert res.status_code == 400


def test_usage_statistics_transmission(logstream):
    client, logStreamName = logstream
    pre_events = client.get_log_events(
        logGroupName=logGroupName,
        logStreamName=logStreamName,
        limit=100,
    )
    assert len(pre_events) < 100, "This test assumed small logstream sizes in the qa stream. Consider changing " \
                                  "fetch limit."

    p = subprocess.Popen(
        ["python", file_relative_path(__file__, "./instantiate_context_with_usage_statistics.py"), "0"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    outs, errs = p.communicate()
    outs = str(outs)
    errs = str(errs)
    assert "INFO" not in outs
    assert "Done constructing a DataContext" in outs
    assert "Ending a long nap" in outs
    assert "KeyboardInterrupt" not in errs

    # Wait a bit for the log events to post
    time.sleep(5)
    post_events = client.get_log_events(
        logGroupName=logGroupName,
        logStreamName=logStreamName,
        limit=100,
    )
    assert len(pre_events["events"]) + 2 == len(post_events["events"])


def test_send_completes_on_kill(logstream):
    client, logStreamName = logstream
    pre_events = client.get_log_events(
        logGroupName=logGroupName,
        logStreamName=logStreamName,
        limit=100,
    )
    """Test that having usage statistics enabled does not negatively impact kill signals or cause loss of queued usage statistics. """
    # Execute process that initializes data context
    acceptable_startup_time = 6
    acceptable_shutdown_time = 1
    nap_time = 30
    start = datetime.datetime.now()
    # Instruct the process to wait for 30 seconds after initializing before completing.
    p = subprocess.Popen(
        ["python", file_relative_path(__file__, "./instantiate_context_with_usage_statistics.py"),
         str(nap_time), "False", "True"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )

    time.sleep(acceptable_startup_time)

    # Send a signal to kill
    p.send_signal(signal.SIGINT)
    outs, errs = p.communicate()
    end = datetime.datetime.now()

    # Ensure that the process shut down earlier than it would have
    assert datetime.timedelta(
        seconds=acceptable_startup_time
    ) < (end - start) < datetime.timedelta(
        seconds=acceptable_startup_time + acceptable_shutdown_time)

    outs = str(outs)
    errs = str(errs)
    assert "INFO" not in outs
    assert "Done constructing a DataContext" in outs
    assert "Ending a long nap" not in outs
    assert "KeyboardInterrupt" in errs
    time.sleep(5)
    post_events = client.get_log_events(
        logGroupName=logGroupName,
        logStreamName=logStreamName,
        limit=100,
    )
    assert len(pre_events["events"]) + 2 == len(post_events["events"])


def test_graceful_failure_with_no_internet():
    """Test that having usage statistics enabled does not negatively impact kill signals or cause loss of queued usage statistics. """

    # Execute process that initializes data context
    # NOTE - JPC - 20200227 - this is crazy long (not because of logging I think, but worth revisiting)
    acceptable_startup_time = 6
    acceptable_shutdown_time = 1
    nap_time = 0
    start = datetime.datetime.now()
    # Instruct the process to wait for 30 seconds after initializing before completing.
    p = subprocess.Popen(
        ["python", file_relative_path(__file__, "./instantiate_context_with_usage_statistics.py"),
         str(nap_time), "True", "True"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    outs, errs = p.communicate()
    end = datetime.datetime.now()
    # We didn't wait or send a signal, so just check that times were reasonable
    assert (end - start) < datetime.timedelta(
        seconds=acceptable_startup_time + acceptable_shutdown_time)
    outs = str(outs)
    errs = str(errs)
    assert "INFO" not in outs
    assert "Done constructing a DataContext" in outs
    assert "Ending a long nap" in outs