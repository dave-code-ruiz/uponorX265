# JNAP with network error retries

import asyncio
import json
import aiohttp
from homeassistant.exceptions import HomeAssistantError

REQUEST_RETRIES = 2
RETRY_DELAY_SECONDS = 1
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=7, connect=2, sock_connect=2, sock_read=5)

class UponorJnap:
    def __init__(self, host, session: aiohttp.ClientSession):
        self.url = "http://" + host + "/JNAP/"
        self.session = session

    async def get_data(self):
        res = await self.post(headers={"x-jnap-action": "http://phyn.com/jnap/uponorsky/GetAttributes"}, payload={})
        output = res.get("output")
        if not isinstance(output, dict):
            raise ValueError(f"Unexpected JNAP response: missing 'output'. keys={list(res.keys())}")

        vars_list = output.get("vars")
        if not isinstance(vars_list, list):
            raise ValueError("Unexpected JNAP response: 'output.vars' missing or invalid")

        return {
            item["waspVarName"]: item["waspVarValue"]
            for item in vars_list
            if isinstance(item, dict) and "waspVarName" in item and "waspVarValue" in item
        }

    async def send_data(self, data):
        payload = {
            "vars": [
                {
                    "waspVarName": key,
                    "waspVarValue": str(data[key]),
                }
                for key in data.keys()
            ]
        }

        r_json = await self.post(headers={"x-jnap-action": "http://phyn.com/jnap/uponorsky/SetAttributes"}, payload=payload)
        if r_json.get("result") != "OK":
            raise ValueError(r_json)

    async def post(self, headers, payload):
        last_error = None
        for attempt in range(REQUEST_RETRIES + 1):
            try:
                async with self.session.post(
                    self.url,
                    headers=headers,
                    json=payload,
                    ssl=False,
                    timeout=REQUEST_TIMEOUT,
                ) as response:
                    response.raise_for_status()
                    return await response.json()
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
                last_error = error
                if attempt < REQUEST_RETRIES:
                    await asyncio.sleep(RETRY_DELAY_SECONDS)
                    continue
                raise HomeAssistantError(f"POST {self.url} failed: {last_error}") from error
