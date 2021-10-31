
"""
nightfall.api
~~~~~~~~~~~~~
    This module provides a class which abstracts the Nightfall REST API.
"""
from datetime import datetime, timedelta
import hmac
import hashlib
import json
import logging
import os
import requests

from nightfall.detection_rules import DetectionRule
from nightfall.exceptions import NightfallUserError, NightfallSystemError


class Nightfall:
    PLATFORM_URL = "https://api.nightfall.ai"
    TEXT_SCAN_ENDPOINT_V3 = PLATFORM_URL + "/v3/scan"
    FILE_SCAN_INITIALIZE_ENDPOINT = PLATFORM_URL + "/v3/upload"
    FILE_SCAN_UPLOAD_ENDPOINT = PLATFORM_URL + "/v3/upload/{0}"
    FILE_SCAN_COMPLETE_ENDPOINT = PLATFORM_URL + "/v3/upload/{0}/finish"
    FILE_SCAN_SCAN_ENDPOINT = PLATFORM_URL + "/v3/upload/{0}/scan"

    def __init__(self, key: str = None, signing_secret: str = None):
        """Instantiate a new Nightfall object.
        :param key: Your Nightfall API key. If None it will be read from the environment variable NIGHTFALL_API_KEY.
        :type key: str or None
        :param signing_secret: Your Nightfall signing secret used for webhook validation.
        :type signing_secret: str or None
        """
        if key:
            self.key = key
        else:
            self.key = os.getenv("NIGHTFALL_API_KEY")

        if not key:
            raise NightfallUserError("need an API key either in constructor or in NIGHTFALL_API_KEY environment var")

        self._headers = {
            "Content-Type": "application/json",
            "User-Agent": "nightfall-python-sdk/1.0.0",
            'Authorization': f'Bearer {self.key}',
        }
        self.signing_secret = signing_secret
        self.logger = logging.getLogger(__name__)

    def scan_text(self, texts: list[str], detection_rule_uuids: list[str] = None,
                  detection_rules: list[DetectionRule] = None):
        """Scan text with Nightfall.

        This method takes the specified config and then makes
        one or more requests to the Nightfall API for scanning.

        Either detection_rule_uuids or DetectionRule is required.
        ::
            detection_rule_uuids: ["uuid",]
            detection_rules: [DetectionRule,]

        :param texts: List of strings to scan.
        :type texts: list[str]
        :param detection_rule_uuids: List of detection rule UUIDs to scan each text with.
            These can be created in the Nightfall UI.
        :type detection_rule_uuids: list[str] or None
        :param detection_rules: List of detection rules to scan each text with.
        :type detection_rules: list[DetectionRule] or None
        :returns: list of findings, list of redacted input texts
        """

        if not detection_rule_uuids and not detection_rules:
            raise NightfallUserError("Need to supply detection rule ids list or detection rules dict with \
                key 'detection_rule_uuids' or 'detection_rules' respectively", 40001)

        config = {}
        if detection_rule_uuids:
            config["detectionRuleUUIDs"] = detection_rule_uuids
        if detection_rules:
            config["detectionRules"] = [d.as_dict() for d in detection_rules]
        request_body = {
            "payload": texts,
            "config": config
        }
        response = self._scan_text_v3(request_body)

        _validate_response(response, 200)

        parsed_response = response.json()

        return parsed_response["findings"], parsed_response["redactedPayload"]

    def _scan_text_v3(self, data):
        response = requests.post(
            url=self.TEXT_SCAN_ENDPOINT_V3,
            headers=self._headers,
            data=json.dumps(data)
        )

        # Logs for Debugging
        self.logger.debug(f"HTTP Request URL: {response.request.url}")
        self.logger.debug(f"HTTP Request Body: {response.request.body}")
        self.logger.debug(f"HTTP Request Headers: {response.request.headers}")

        self.logger.debug(f"HTTP Status Code: {response.status_code}")
        self.logger.debug(f"HTTP Response Headers: {response.headers}")
        self.logger.debug(f"HTTP Response Text: {response.text}")

        return response

    # File Scan

    def scan_file(self, location: str, webhook_url: str, policy_uuid: str = None,
                  detection_rule_uuids: list[str] = None, detection_rules: list[DetectionRule] = None):
        """Scan file with Nightfall.

        Either policy_uuid or detection_rule_uuids or detection_rules is required.
        ::
            policy_uuid: "uuid"
            detection_rule_uuids: ["uuid",]
            detection_rules: [{detection_rule},]

        :param location: location of file to scan.
        :param webhook_url: webhook endpoint which will receive the results of the scan.
        :param policy_uuid: policy UUID.
        :type policy_uuid: str or None
        :param detection_rule_uuids: list of detection rule UUIDs.
        :type detection_rule_uuids: list[str] or None
        :param detection_rules: list of detection rules.
        :type detection_rules: list[DetectionRule] or None
        """

        if not policy_uuid and not detection_rule_uuids and not detection_rules:
            raise NightfallUserError("Need to supply policy id or detection rule ids list or detection rules dict with \
                key 'policy_uuid', 'detection_rule_uuids', 'detection_rules' respectively", 40001)

        response = self._file_scan_initialize(location)
        _validate_response(response, 200)
        result = response.json()
        session_id, chunk_size = result['id'], result['chunkSize']

        uploaded = self._file_scan_upload(session_id, location, chunk_size)
        if not uploaded:
            raise NightfallSystemError("File upload failed", 50000)

        response = self._file_scan_finalize(session_id)
        _validate_response(response, 200)

        response = self._file_scan_scan(session_id, webhook_url,
                                        policy_uuid=policy_uuid,
                                        detection_rule_uuids=detection_rule_uuids,
                                        detection_rules=detection_rules)
        _validate_response(response, 200)

        return response.json()

    def _file_scan_initialize(self, location: str):
        data = {
            "fileSizeBytes": os.path.getsize(location)
        }
        response = requests.post(
            url=self.FILE_SCAN_INITIALIZE_ENDPOINT,
            headers=self._headers,
            data=json.dumps(data)
        )

        return response

    def _file_scan_upload(self, session_id, location: str, chunk_size: int):

        def read_chunks(fp, chunk_size):
            ix = 0
            while True:
                data = fp.read(chunk_size)
                if not data:
                    break
                yield ix, data
                ix = ix + 1

        def upload_chunk(id, data, headers):
            response = requests.patch(
                url=self.FILE_SCAN_UPLOAD_ENDPOINT.format(id),
                data=data,
                headers=headers
            )
            return response

        with open(location) as fp:
            for ix, piece in read_chunks(fp, chunk_size):
                headers = self._headers
                headers["X-UPLOAD-OFFSET"] = str(ix * chunk_size)
                response = upload_chunk(session_id, piece, headers)
                _validate_response(response, 204)

        return True

    def _file_scan_finalize(self, session_id):
        response = requests.post(
            url=self.FILE_SCAN_COMPLETE_ENDPOINT.format(session_id),
            headers=self._headers
        )
        return response

    def _file_scan_scan(self, session_id: str, webhook_url: str, policy_uuid: str,
                        detection_rule_uuids: str, detection_rules: list[DetectionRule]):
        if policy_uuid:
            data = {"policyUUID": policy_uuid}
        else:
            data = {"policy": {"webhookURL": webhook_url}}
            if detection_rule_uuids:
                data["detectionRuleUUIDs"] = detection_rule_uuids
            if detection_rules:
                data["detectionRules"] = [d.as_dict() for d in detection_rules]

        response = requests.post(
            url=self.FILE_SCAN_SCAN_ENDPOINT.format(session_id),
            headers=self._headers,
            data=json.dumps(data)
        )
        return response

    def validate_webhook(self, request_signature: str, request_timestamp: str, request_data: str):
        """
        Validate the integrity of webhook requests coming from Nightfall.

        :param request_signature: value of X-Nightfall-Signature header
        :type request_signature: str
        :param request_timestamp: value of X-Nightfall-Timestamp header
        :type request_timestamp: str
        :param request_data: request body as a unicode string
            Flask: request.get_data(as_text=True)
            Django: request.body.decode("utf-8")
        :type request_data: str
        :returns: validation status boolean
        """

        now = datetime.now()
        if now-timedelta(minutes=5) <= datetime.fromtimestamp(int(request_timestamp)) <= now:
            raise NightfallUserError("could not validate timestamp is within the last few minutes", 40000)
        computed_signature = hmac.new(
            self.signing_secret.encode(),
            msg=F"{request_timestamp}:{request_data}".encode(),
            digestmod=hashlib.sha256
        ).hexdigest().lower()
        if computed_signature != request_signature:
            raise NightfallUserError("could not validate signature of inbound request!", 40000)
        return True


# Utility
def _validate_response(response: requests.Response, expected_status_code: int):
    if response.status_code == expected_status_code:
        return
    response_json = response.json()
    error_code = response_json.get('code', None)
    if error_code is not None:
        if error_code < 40000 or error_code >= 50000:
            raise NightfallSystemError(response.text, error_code)
        else:
            raise NightfallUserError(response.text, error_code)
    else:
        raise NightfallSystemError(response.text, 50000)
