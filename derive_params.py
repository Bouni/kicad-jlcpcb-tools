"""Derive parameters from the part description and package."""

# LCSC hides the critical parameters (like resistor values) in the middle of the
# description field, which makes them invisible in the footprint list. This
# makes it very tedious to double-check the part mappings, as the part details
# dialog has to be opened for each one.
#
# This function uses heuristics to extract a human-readable summary of the most
# significant parameters of the parts from the LCSC data so they can be displayed
# separately in the footprint list.

import logging
import re

logger = logging.getLogger()
logging.basicConfig(encoding="utf-8", level=logging.DEBUG)


def params_for_part(part) -> str:
    """Derive parameters from the part description and package."""
    description = part.get("description", "")
    category = part.get("category", "")
    part_no = part.get("part_no", "")
    package = part.get("package", "")

    result = []

    # These are heuristic regexes to pull out parameters like resistance,
    # capacitance, voltage, current, etc. from the description field. As LCSC
    # makes random changes to the format of the descriptions, this function will
    # need to follow along.
    #
    # The package size isn't always in the description, but will be added by the
    # caller.

    # For passives, focus on generic values like resistance, capacitance, voltage

    if "Resistors" in category:
        result.extend(re.findall(r"([.\d]+[mkM]?Ω)", description))
        result.extend(re.findall(r"(±[.\d]+%)", description))
    elif "Capacitors" in category:
        result.extend(re.findall(r"([.\d]+[pnmuμ]?F)", description))
        result.extend(re.findall(r"([.\d]+[mkM]?V)", description))
    elif "Inductors" in category:
        result.extend(re.findall(r"([.\d]+[nuμm]?H)", description))
        result.extend(re.findall(r"([.\d]+m?A)", description))

    # For diodes, we may be looking for specific part or generic I/V specs

    elif "Diodes" in category:
        if part_no:
            result.append(part_no)
        result.extend(re.findall(r"(?<!@)\b([.\d]+[mkM]?[AW])\b", description))
        result.extend(
            re.findall(r"(?<!@)\b([.\d]+[mk]?V(?:~[.\d]+[mk]?V)?)(?!@)", description)
        )
        result.extend(re.findall(r"Schottky|Fast|Dual", description))

    # For LEDs, check the color

    elif "Optoelectronics" in category:
        result.extend(
            re.findall(
                r"(red|green|blue|amber|emerald|white|yellow)",
                description,
                re.IGNORECASE,
            )
        )

    # For other types, just show the part number

    elif part_no:
        result.append(part_no)

    if package != "":
        result.append(package)

    return " ".join(result)


# Test cases from actual LCSC data
#
# Generate random samples from the parts DB with:
#
# SELECT "LCSC Part", "Description", "First Category", "Second Category"
# FROM parts
# WHERE ROWID IN (
#   SELECT ROWID FROM parts
#   where "First Category" match "Transistors"
#   ORDER BY RANDOM() LIMIT 10
# )


def test_params_for_part():
    """Test cases from actual LCSC data."""
    test_cases = {
        "Resistors": [
            ("250mW Thin Film Resistor 200V ±0.1% ±25ppm/℃ 284kΩ", "284kΩ ±0.1%"),
            ("Metal Film Resistors 357kΩ 400mW ±50ppm/℃ ±1%", "357kΩ ±1%"),
            ("Wirewound Resistors 800Ω 13W ±30ppm/℃ ±5%", "800Ω ±5%"),
            ("7W ±75ppm/℃ ±1% 200mΩ", "200mΩ ±1%"),
            ("500mW Thick Film Resistors ±100ppm/℃ ±1% 365Ω", "365Ω ±1%"),
            ("250mW ±0.1% ±100ppm/℃ 6.04kΩ", "6.04kΩ ±0.1%"),
            ("±20% 250mW 1kΩ   Potentiometers, Variable Resistors", "1kΩ ±20%"),
            ("Carbon Resister 3.3kΩ 2W -500ppm/℃~0ppm/℃ ±10%", "3.3kΩ ±10%"),
            ("47.04kΩ ±50ppm/℃ ±1%", "47.04kΩ ±1%"),
            ("2 ±5% 4.3kΩ 62.5mW ±200ppm/℃", "4.3kΩ ±5%"),
        ],
        "Capacitors": [
            ("16V 68nF X7R ±20%", "68nF 16V"),
            ("1kV 33pF null ±10%", "33pF 1kV"),
            ("25V 100nF ±5%", "100nF 25V"),
            ("150V 8.2pF", "8.2pF 150V"),
            ("±10% 1.5nF R 2kV   Through Hole Ceramic Capacitors", "1.5nF 2kV"),
            ("100V 120pF NP0 ±2%", "120pF 100V"),
            ("10V 22uF X6S ±20%", "22uF 10V"),
            ("100uF 15V 180mΩ ±10%", "100uF 15V"),
        ],
        "Inductors": [
            ("3A 18.5nH ±5%", "18.5nH 3A"),
            ("175mA 12uH ±5%", "12uH 175mA"),
            ("600mA 1.4nH 150mΩ", "1.4nH 600mA"),
            ("6.4A 6uH ±25% 15A", "6uH 6.4A 15A"),
        ],
        "Diodes": [
            ("1W 82V", "1W 82V"),
            ("500mW 8.2V", "500mW 8.2V"),
            (
                "16V 1 pair of common cathodes 1V@35mA 75mA   Schottky Diodes",
                "75mA 16V Schottky",
            ),
            ("150V 875mV@1A 25ns 1A   s", "1A 150V"),
            (
                "100uA@100V 100V Dual Common Cathode 950mV@20A 20A TO-220AB",
                "20A 100V Dual",
            ),
            ("45V 15A 580mV@15A   Schottky Diodes", "15A 45V Schottky"),
            ("35V 100mA 300mV@10mA   Schottky Diodes", "100mA 35V Schottky"),
            (
                "1.7V@2A 100ns 2A 1kV  Fast Recovery / High Efficiency Diodes",
                "2A 1kV Fast",
            ),
            ("40V Independent Type 450mV@3A 3A  Schottky Diodes", "3A 40V Schottky"),
            ("Independent Type 5.8V~6.6V 300mW 6.2V", "300mW 5.8V~6.6V 6.2V"),
            ("6.2V~6.6V 200mW 5.8V", "200mW 6.2V~6.6V 5.8V"),
        ],
        "Optoelectronics": [
            ("Blue  LED Indication - Discrete", "Blue"),
            ("Emerald,Blue  LED Indication - Discrete", "Emerald Blue"),
            ("350mA 7000K White 125° 2.73V", "White"),
        ],
        "Other": [
            ("doesn't matter", ""),
        ],
    }

    for category, tests in test_cases.items():
        for description, parsed_params in tests:
            result = params_for_part(
                {"description": description, "category": category, "package": "thepkg"}
            )
            expected = f"{parsed_params} thepkg" if parsed_params else "thepkg"
            assert result == expected, (
                f"For {description}: expected {expected}, got {result}"
            )
    logger.info("All tests passed.")


# Run the tests if this file was run as a script
if __name__ == "__main__":
    test_params_for_part()
