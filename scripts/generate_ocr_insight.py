# -*- coding: utf-8 -*-
import contextlib
import enum
import gzip
import re
import argparse
import json
import sys

import pathlib as pathlib
from typing import List, Dict, Any, Iterable, Optional, Tuple, Callable

import requests


def process_fr_packaging_match(match) -> str:
    country_code, *approval_numbers, ec = match.group(1, 2, 3, 4, 5)
    return "{} {}.{}.{} {}".format(country_code, *approval_numbers, ec)


def process_fr_emb_match(match) -> str:
    emb_str, city_code, company_code = match.group(1, 2, 3)
    city_code = city_code.replace(' ', '')
    company_code = company_code or ''
    return "{} {}{}".format(emb_str, city_code, company_code)


def process_eu_bio_label_code(match) -> str:
    return "{}-{}-{}".format(match.group(1),
                             match.group(2),
                             match.group(3))


class OCRField(enum.Enum):
    full_text = 1
    full_text_contiguous = 2
    text_annotations = 3


class OCRRegex:
    __slots__ = ('regex', 'field', 'lowercase', 'processing_func')

    def __init__(self, regex,
                 field: OCRField,
                 lowercase: bool=False,
                 processing_func: Optional[Callable] = None):
        self.regex = regex
        self.field: OCRField = field
        self.lowercase: bool = lowercase
        self.processing_func = processing_func


NUTRISCORE_REGEX = re.compile(r"nutri[-\s]?score", re.IGNORECASE)
WEIGHT_MENTIONS = (
    "poids net:",
    "poids net égoutté:",
    "net weight:",
    "peso neto:",
    "peso liquido:",
    "netto gewicht:",
)

WEIGHT_MENTIONS_RE = re.compile('|'.join((re.escape(x)
                                          for x in WEIGHT_MENTIONS)),
                                re.IGNORECASE)

WEIGHT_VALUES_REGEX = re.compile(
    r"([0-9]+[,.]?[0-9]*)\s*(fl oz|dl|cl|mg|mL|lbs|oz|g|kg|L)(?![^\s])")

URL_REGEX = re.compile(r'^(http://www\.|https://www\.|http://|https://)?[a-z0-9]+([\-.]{1}[a-z0-9]+)*\.[a-z]{2,5}(:[0-9]{1,5})?(/.*)?$')
EMAIL_REGEX = re.compile(r'[\w.-]+@[\w.-]+')
PHONE_REGEX = re.compile(r'\d{3}[-.\s]??\d{3}[-.\s]??\d{4}|\(\d{3}\)\s*\d{3}[-.\s]??\d{4}|\d{3}[-.\s]??\d{4}')

PACKAGER_CODE: Dict[str, OCRRegex] = {
    "fr_emb": OCRRegex(re.compile(r"(EMB) ?(\d ?\d ?\d ?\d ?\d)([a-zA-Z]{1,2})?"),
                       field=OCRField.text_annotations,
                       lowercase=True,
                       processing_func=process_fr_emb_match),
    "eu_fr": OCRRegex(re.compile("(FR) (\d{1,3})[\-\s.](\d{1,3})[\-\s.](\d{1,3}) (CE|EC)"),
                      field=OCRField.full_text_contiguous,
                      lowercase=True,
                      processing_func=process_fr_packaging_match),
}

RECYCLING_REGEX = {
    'recycling': [
        re.compile(r"recycle", re.IGNORECASE),
    ],
    'throw_away': [
        re.compile(r"(?:throw away)|(?:jeter)", re.IGNORECASE)
    ]
}

LABELS_REGEX = {
    'en:organic': [
        OCRRegex(re.compile(r"ingr[ée]dients?\sbiologiques?", re.IGNORECASE),
                 field=OCRField.full_text_contiguous,
                 lowercase=True),
        OCRRegex(re.compile(r"ingr[ée]dients?\sbio[\s.,)]"),
                 field=OCRField.full_text_contiguous,
                 lowercase=True),
        OCRRegex(re.compile(r"agriculture ue/non ue biologique"),
                 field=OCRField.full_text_contiguous,
                 lowercase=True),
        OCRRegex(re.compile(r"agriculture bio(?:logique)?[\s.,)]"),
                 field=OCRField.full_text_contiguous,
                 lowercase=True),
        OCRRegex(re.compile(r"production bio(?:logique)?[\s.,)]"),
                 field=OCRField.full_text_contiguous,
                 lowercase=True),
    ],
    'xx-bio-xx': [
        OCRRegex(re.compile(r"([A-Z]{2})[\-\s.](BIO|ÖKO)[\-\s.](\d{2,3})"),
                 field=OCRField.text_annotations,
                 lowercase=False,
                 processing_func=process_eu_bio_label_code),
    ],
    'fr:ab-agriculture-biologique': [
        OCRRegex(re.compile(r"certifi[ée] ab[\s.,)]"),
                 field=OCRField.full_text_contiguous,
                 lowercase=True),
    ]
}

BEST_BEFORE_DATE_REGEX = {
    'en': re.compile(r'\d\d\s(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)(?:\s\d{4})?', re.IGNORECASE),
    'fr': re.compile(r'\d\d\s(?:Jan|Fev|Mar|Avr|Mai|Juin|Juil|Aou|Sep|Oct|Nov|Dec)(?:\s\d{4})?', re.IGNORECASE),
    'full_digits': re.compile(r'\d{2}[./]\d{2}[./](?:\d{2}){1,2}'),
}


class OCRResult:
    __slots__ = ('text_annotations', 'full_text_annotation')

    def __init__(self, data: Dict[str, Any]):
        self.text_annotations: List[OCRTextAnnotation] = []
        self.full_text_annotation: OCRFullTextAnnotation = None

        for text_annotation_data in data.get('textAnnotations', []):
            text_annotation = OCRTextAnnotation(text_annotation_data)
            self.text_annotations.append(text_annotation)

        full_text_annotation_data = data.get('fullTextAnnotation')

        if full_text_annotation_data:
            self.full_text_annotation = OCRFullTextAnnotation(full_text_annotation_data)

    def get_full_text(self, lowercase: bool = False) -> Optional[str]:
        if self.full_text_annotation is not None:
            if lowercase:
                return self.full_text_annotation.text.lower()

            return self.full_text_annotation.text

        return

    def get_full_text_contiguous(self, lowercase: bool = False) -> Optional[str]:
        if self.full_text_annotation is not None:
            if lowercase:
                return self.full_text_annotation.contiguous_text.lower()

            return self.full_text_annotation.contiguous_text

        return

    def iter_text_annotations(self, lowercase: bool = False) -> Iterable[str]:
        for text_annotation in self.text_annotations:
            if lowercase:
                yield text_annotation.text.lower()

            yield text_annotation.text

    def get_text(self, ocr_regex: OCRRegex) -> Iterable[str]:
        field = ocr_regex.field

        if field == OCRField.full_text:
            text = self.get_full_text(ocr_regex.lowercase)

            if text:
                return [text]

        elif field == OCRField.full_text_contiguous:
            text = self.get_full_text_contiguous(ocr_regex.lowercase)

            if text:
                return [text]

        elif field == OCRField.text_annotations:
            return list(self.iter_text_annotations(ocr_regex.lowercase))

        else:
            raise ValueError("invalid field: {}".format(field))

        return []


class OCRFullTextAnnotation:
    __slots__ = ('text', 'pages', 'contiguous_text')

    def __init__(self, data: Dict[str, Any]):
        self.text = data['text']
        self.contiguous_text = self.text.replace('\n', ' ')
        self.pages = []


class OCRTextAnnotation:
    __slots__ = ('locale', 'text', 'bounding_poly')

    def __init__(self, data: Dict[str, Any]):
        self.locale = data.get('locale')
        self.text = data['description']
        self.bounding_poly = [(point.get('x', 0), point.get('y', 0)) for point in data['boundingPoly']['vertices']]


def get_barcode_from_path(path: str):
    path = pathlib.Path(path)

    barcode = ''

    for parent in path.parents:
        if parent.name.isdigit():
            barcode = parent.name + barcode
        else:
            break

    barcode = barcode or None
    return barcode


def split_barcode(barcode: str) -> List[str]:
    if len(barcode) == 13:
        return [barcode[0:3], barcode[3:6], barcode[6:9], barcode[9:13]]
    elif len(barcode) == 8:
        return [barcode]

    raise ValueError("unknown barcode format: {}".format(barcode))


def generate_image_url(barcode: str, image_name: str) -> str:
    splitted_barcode = split_barcode(barcode)
    path = "/{}/{}.json".format('/'.join(splitted_barcode), image_name)
    return "https://static.openfoodfacts.org/images/products" + path


def fetch_images_for_ean(ean: str):
    url = "https://world.openfoodfacts.org/api/v0/product/" \
          "{}.json?fields=images".format(ean)
    images = requests.get(url).json()
    return images


def get_json_for_image(barcode: str, image_name: str) -> \
        Optional[Dict[str, Any]]:
    url = generate_image_url(barcode, image_name)
    r = requests.get(url)

    if r.status_code == 404:
        return

    return r.json()


def get_ocr_response(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    responses = data.get('responses', [])

    if not responses:
        return

    response = responses[0]

    if 'error' in response:
        return

    return response


def find_emails(text: str) -> List[Dict]:
    results = []

    for match in EMAIL_REGEX.finditer(text):
        results.append({
            "text": match.group(),
        })

    return results


def find_urls(text: str) -> List[Dict]:
    results = []
    for match in URL_REGEX.finditer(text):
        results.append({
            "text": match.group(),
        })

    return results


def find_packager_codes(ocr_result: OCRResult) -> List[Dict]:
    results = []

    for regex_code, ocr_regex in PACKAGER_CODE.items():
        for text in ocr_result.get_text(ocr_regex):
            for match in ocr_regex.regex.finditer(text):
                value = ocr_regex.processing_func(match)
                results.append({
                    "raw": match.group(0),
                    "text": value,
                    "type": regex_code,
                })

    return results


def find_weight_values(text: str) -> List[Dict]:
    weight_values = []

    for match in WEIGHT_VALUES_REGEX.finditer(text):
        result = {
            'text': match.group(),
            'value': match.group(1),
            'unit': match.group(2),
        }
        weight_values.append(result)

    return weight_values


def find_weight_mentions(text: str) -> List[Dict]:
    weight_mentions = []

    for match in WEIGHT_MENTIONS_RE.finditer(text):
        result = {
            'text': match.group(),
        }
        weight_mentions.append(result)

    return weight_mentions


TEMPERATURE_REGEX_STR = r"[+-]?\s*\d+\s*°?C"
TEMPERATURE_REGEX = re.compile(r"(?P<value>[+-]?\s*\d+)\s*°?(?P<unit>C)",
                               re.IGNORECASE)

STORAGE_INSTRUCTIONS_REGEX = {
    'max': re.compile(r"[aà] conserver [àa] ({0}) maximum".format(
        TEMPERATURE_REGEX_STR), re.IGNORECASE),
    'between': re.compile(r"[aà] conserver entre ({0}) et ({0})".format(
        TEMPERATURE_REGEX_STR), re.IGNORECASE),
}


def extract_temperature_information(temperature: str) -> Dict:
    match = TEMPERATURE_REGEX.match(temperature)

    if match:
        result = {}
        value = match.group('value')
        unit = match.group('unit')

        if value:
            result['value'] = value

        if unit:
            result['unit'] = unit

        return result


def find_storage_instructions(text: str) -> List[Dict]:
    text = text.lower()

    results = []

    for instruction_type, regex in STORAGE_INSTRUCTIONS_REGEX.items():
        for match in regex.finditer(text):
            if match:
                result = {
                    'text': match.group(),
                    'type': instruction_type,
                }

                if instruction_type == 'max':
                    result['max'] = extract_temperature_information(
                        match.group(1))

                elif instruction_type == 'between':
                    result['between'] = {
                        'min': extract_temperature_information(match.group(1)),
                        'max': extract_temperature_information(match.group(2)),
                    }

                results.append(result)

    return results


def find_nutriscore(text: str) -> List[Dict]:
    results = []
    for match in NUTRISCORE_REGEX.finditer(text):
        results.append({
            "text": match.group(),
        })

    return results


def find_phone_numbers(text) -> List[Dict]:
    results = []

    for match in PHONE_REGEX.finditer(text):
        results.append({
            "text": match.group(),
        })

    return results


def find_recycling_instructions(text) -> List[Dict]:
    results = []

    for instruction_type, regex_list in RECYCLING_REGEX.items():
        for regex in regex_list:
            for match in regex.finditer(text):
                results.append({
                    'type': instruction_type,
                    'text': match.group(),
                })

    return results


def find_labels(ocr_result: OCRResult) -> List[Dict]:
    results = []

    for label_tag, regex_list in LABELS_REGEX.items():
        for ocr_regex in regex_list:
            for text in ocr_result.get_text(ocr_regex):
                for match in ocr_regex.regex.finditer(text):
                    if ocr_regex.processing_func:
                        label_value = ocr_regex.processing_func(match)
                    else:
                        label_value = label_tag

                    results.append({
                        'label_tag': label_value,
                        'text': match.group(),
                    })

    return results


def find_best_before_date(text: str) -> List[Dict]:
    # Parse best_before_date
    #        "À consommer de préférence avant",
    results = []

    for type_, regex in BEST_BEFORE_DATE_REGEX.items():
        for match in regex.finditer(text):
            results.append({
                "text": match.group(),
                "type": type_,
            })

    return results


def extract_insights(ocr_result: OCRResult,
                     insight_type: str) -> List[Dict]:
    # if insight_type == 'weight_value':
    #     return find_weight_values(text)

    # elif insight_type == 'weight_mention':
    #     return find_weight_mentions(text)

    if insight_type == 'packager_code':
        return find_packager_codes(ocr_result)

    # elif insight_type == 'nutriscore':
    #     return find_nutriscore(text)

    # elif insight_type == 'recycling_instruction':
    #     return find_recycling_instructions(contiguous_text)

    # elif insight_type == 'email':
    #     return find_emails(text)

    # elif insight_type == 'url':
    #     return find_urls(text)

    elif insight_type == 'label':
        return find_labels(ocr_result)

    # elif insight_type == 'storage_instruction':
    #     return find_storage_instructions(contiguous_text)

    # elif insight_type == 'best_before_date':
    #     return find_best_before_date(text)
    else:
        raise ValueError("unknown insight type: {}".format(insight_type))


def is_barcode(text: str):
    return len(text) == 13 and text.isdigit()


def get_source(image_name: str, json_path: str = None, barcode: str = None):
    if not barcode:
        barcode = get_barcode_from_path(str(json_path))

    return "/{}/{}.jpg" \
           "".format('/'.join(split_barcode(barcode)),
                     image_name)


def ocr_iter(input_str: str) -> Iterable[Tuple[Optional[str], Dict]]:
    if is_barcode(input_str):
        image_data = fetch_images_for_ean(input_str)['product']['images']

        for image_name in image_data.keys():
            if image_name.isdigit():
                print("Getting OCR for image {}".format(image_name))
                data = get_json_for_image(input_str, image_name)
                source = get_source(image_name, barcode=input_str)
                if data:
                    yield source, data

    else:
        input_path = pathlib.Path(input_str)

        if not input_path.exists():
            print("Unrecognized input: {}".format(input_path))
            return

        if input_path.is_dir():
            for json_path in input_path.glob("**/*.json"):
                with open(str(json_path), 'r') as f:
                    source = get_source(json_path.stem,
                                        json_path=str(json_path))
                    yield source, json.load(f)
        else:
            if '.json' in input_path.suffixes:
                with open(str(input_path), 'r') as f:
                    yield None, json.load(f)

            elif '.jsonl' in input_path.suffixes:
                if input_path.suffix == '.gz':
                    open_func = gzip.open
                else:
                    open_func = open

                with open_func(input_path, mode='rt') as f:
                    for line in f:
                        json_data = json.loads(line)

                        if 'content' in json_data:
                            source = json_data['source'].replace('//', '/')
                            yield source, json_data['content']


def get_ocr_from_barcode(barcode: str):
    image_data = fetch_images_for_ean(barcode)['product']['images']

    for image_name in image_data.keys():
        if image_name.isdigit():
            print("Getting OCR for image {}".format(image_name))
            data = get_json_for_image(barcode, image_name)
            return data


def run(args: argparse.Namespace):
    input_ = args.input

    if args.output is not None:
        output = open(args.output, 'w')
    else:
        output = sys.stdout

    with contextlib.closing(output):
        for source, ocr_json in ocr_iter(input_):
            ocr_response = get_ocr_response(ocr_json)

            if not ocr_response:
                continue

            ocr_result: OCRResult = OCRResult(ocr_response)
            insights = extract_insights(ocr_result, args.insight_type)

            if insights:
                item = {
                    'insights': insights,
                    'barcode': get_barcode_from_path(source),
                    'type': args.insight_type,
                }

                if source:
                    item['source'] = source

                output.write(json.dumps(item) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('input')
    parser.add_argument('--insight-type', required=True)
    parser.add_argument('--output', '-o')
    arguments = parser.parse_args()
    run(arguments)