# JNAP with network error retries

import json
import requests
from requests.adapters import HTTPAdapter, Retry

REQUEST_RETRIES = 10 # retry attempts
BACKOFF_FACTOR = 3 # exponential backoff

class UponorJnap:
    def __init__(self, host):
        self.url = "http://" + host + "/JNAP/"
        self.session = requests.Session()
        self.session.mount('http://', HTTPAdapter(max_retries=Retry(
            total=REQUEST_RETRIES,
            connect=REQUEST_RETRIES,
            read=REQUEST_RETRIES,
            backoff_factor=BACKOFF_FACTOR,
        )))

    def get_data(self):
        res = self.post(headers={"x-jnap-action": "http://phyn.com/jnap/uponorsky/GetAttributes"}, payload={})
        return dict(map(lambda v: (v["waspVarName"], v["waspVarValue"]), res["output"]["vars"]))

    def send_data(self, data):
        payload = {
            "vars": list(map(lambda k: {
                "waspVarName": k,
                "waspVarValue": data[k],
            }, data.keys()))
        }
        r_json = self.post(headers={"x-jnap-action": "http://phyn.com/jnap/uponorsky/SetAttributes"}, payload=payload)
        if 'result' in r_json and not r_json['result'] == 'OK':
            raise ValueError(r_json)

    def post(self, headers, payload):
      requests.packages.urllib3.disable_warnings()
      try:
          res = self.session.post(self.url, headers=headers, json=payload, verify=False)
          if res.status_code != 200:
              raise Exception("Status code: {}".format(res.status_code))
          return res.json()
      except Exception as e:
          raise Exception("POST {} failed: {}".format(self.url, e))
