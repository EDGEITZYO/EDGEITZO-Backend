from typing import Any

import xmltodict


def parse_scienceon_xml(xml_text: str) -> dict[str, Any]:
    parsed = xmltodict.parse(xml_text)
    return parsed