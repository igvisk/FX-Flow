from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

APP_NAME = "FX-Flow"
APP_VERSION = "1.4.2"
AUTHOR = "Igor Vitovský"
GITHUB = "github.com/igvisk"

DEFAULT_LANGUAGE = "sk"
FALLBACK_LANGUAGE = "en"
LANGUAGE_CODES = ("en", "sk", "cs", "de", "fr", "it", "es", "ru", "pl")
LANGUAGE_SHORT_LABELS = {
    "en": "EN",
    "sk": "SK",
    "cs": "CS",
    "de": "DE",
    "fr": "FR",
    "it": "IT",
    "es": "ES",
    "ru": "RU",
    "pl": "PL",
}


@dataclass(slots=True)
class RateSnapshot:
    base: str
    date: str
    rates: dict[str, float]
    source: str
    cached_at: str | None = None

    def to_cache_record(self) -> dict[str, Any]:
        return {
            "base": self.base,
            "date": self.date,
            "rates": self.rates,
            "cached_at": self.cached_at,
        }

    @classmethod
    def from_cache_record(cls, base: str, payload: dict[str, Any]) -> "RateSnapshot":
        rates = {
            str(code).upper(): float(value)
            for code, value in dict(payload.get("rates", {})).items()
        }
        rates[base] = 1.0
        return cls(
            base=base,
            date=str(payload.get("date", "")),
            rates=rates,
            source="cache",
            cached_at=payload.get("cached_at"),
        )


@dataclass(slots=True)
class AppPreferences:
    favorites: list[str] = field(default_factory=lambda: ["EUR", "USD", "CZK", "TRY"])
    last_from: str = "EUR"
    last_to: str = "USD"
    last_amount: str = "1"
    language: str = "sk"

    def to_dict(self) -> dict[str, Any]:
        return {
            "favorites": list(dict.fromkeys(code.upper() for code in self.favorites)),
            "last_from": self.last_from.upper(),
            "last_to": self.last_to.upper(),
            "last_amount": self.last_amount,
            "language": self.language.lower(),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "AppPreferences":
        data = payload or {}
        favorites = [
            str(code).upper()
            for code in data.get("favorites", ["EUR", "USD", "CZK", "TRY"])
            if str(code).strip()
        ]
        if not favorites:
            favorites = ["EUR", "USD", "CZK", "TRY"]
        return cls(
            favorites=list(dict.fromkeys(favorites)),
            last_from=str(data.get("last_from", "EUR")).upper(),
            last_to=str(data.get("last_to", "USD")).upper(),
            last_amount=str(data.get("last_amount", "1")),
            language=str(data.get("language", "sk")).lower(),
        )


class LocaleManager:
    def __init__(
        self,
        locales_dir: Path,
        default_language: str = DEFAULT_LANGUAGE,
        fallback_language: str = FALLBACK_LANGUAGE,
    ) -> None:
        self.locales_dir = locales_dir
        self.default_language = default_language
        self.fallback_language = fallback_language
        self.language = self.normalize_language(default_language)
        self._locales = {
            language: self._load_locale(language) for language in LANGUAGE_CODES
        }

    def normalize_language(self, language: str | None) -> str:
        normalized = (language or self.default_language).strip().lower()
        return normalized if normalized in LANGUAGE_CODES else self.default_language

    def set_language(self, language: str | None) -> None:
        self.language = self.normalize_language(language)

    def t(self, key: str, language: str | None = None, **kwargs: object) -> str:
        active_language = self.normalize_language(language or self.language)
        message = (
            self._locales.get(active_language, {}).get(key)
            or self._locales.get(self.fallback_language, {}).get(key)
            or key
        )
        if not kwargs:
            return message
        try:
            return message.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return message

    def _load_locale(self, language: str) -> dict[str, str]:
        path = self.locales_dir / f"{language}.json"
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return {
            str(key): str(value)
            for key, value in payload.items()
            if isinstance(key, str) and isinstance(value, str)
        }


DEFAULT_CURRENCIES = ["EUR", "USD", "CZK", "TRY"]
LEGACY_CACHE_FILE = Path(__file__).resolve().parent / "rates.json"
CURRENCY_NAMES = {
    "AED": "UAE dirham",
    "AFN": "Afghan afghani",
    "ALL": "Albanian lek",
    "AMD": "Armenian dram",
    "ANG": "Netherlands Antillean guilder",
    "AOA": "Angolan kwanza",
    "ARS": "Argentine peso",
    "AUD": "Australian dollar",
    "AWG": "Aruban florin",
    "AZN": "Azerbaijani manat",
    "BAM": "Bosnia and Herzegovina convertible mark",
    "BBD": "Barbadian dollar",
    "BDT": "Bangladeshi taka",
    "BGN": "Bulgarian lev",
    "BHD": "Bahraini dinar",
    "BIF": "Burundian franc",
    "BMD": "Bermudian dollar",
    "BND": "Brunei dollar",
    "BOB": "Bolivian boliviano",
    "BRL": "Brazilian real",
    "BSD": "Bahamian dollar",
    "BTN": "Bhutanese ngultrum",
    "BWP": "Botswana pula",
    "BYN": "Belarusian ruble",
    "BZD": "Belize dollar",
    "CAD": "Canadian dollar",
    "CDF": "Congolese franc",
    "CHF": "Swiss franc",
    "CLP": "Chilean peso",
    "CNY": "Chinese yuan",
    "COP": "Colombian peso",
    "CRC": "Costa Rican colon",
    "CUP": "Cuban peso",
    "CVE": "Cape Verdean escudo",
    "CZK": "Czech koruna",
    "DJF": "Djiboutian franc",
    "DKK": "Danish krone",
    "DOP": "Dominican peso",
    "DZD": "Algerian dinar",
    "EGP": "Egyptian pound",
    "ERN": "Eritrean nakfa",
    "ETB": "Ethiopian birr",
    "EUR": "Euro",
    "FJD": "Fijian dollar",
    "FKP": "Falkland Islands pound",
    "GBP": "Pound sterling",
    "GEL": "Georgian lari",
    "GHS": "Ghanaian cedi",
    "GIP": "Gibraltar pound",
    "GMD": "Gambian dalasi",
    "GNF": "Guinean franc",
    "GTQ": "Guatemalan quetzal",
    "GYD": "Guyanese dollar",
    "HKD": "Hong Kong dollar",
    "HNL": "Honduran lempira",
    "HRK": "Croatian kuna",
    "HTG": "Haitian gourde",
    "HUF": "Hungarian forint",
    "IDR": "Indonesian rupiah",
    "ILS": "Israeli new shekel",
    "INR": "Indian rupee",
    "IQD": "Iraqi dinar",
    "IRR": "Iranian rial",
    "ISK": "Icelandic krona",
    "JMD": "Jamaican dollar",
    "JOD": "Jordanian dinar",
    "JPY": "Japanese yen",
    "KES": "Kenyan shilling",
    "KGS": "Kyrgyzstani som",
    "KHR": "Cambodian riel",
    "KMF": "Comorian franc",
    "KRW": "South Korean won",
    "KWD": "Kuwaiti dinar",
    "KYD": "Cayman Islands dollar",
    "KZT": "Kazakhstani tenge",
    "LAK": "Lao kip",
    "LBP": "Lebanese pound",
    "LKR": "Sri Lankan rupee",
    "LRD": "Liberian dollar",
    "LSL": "Lesotho loti",
    "LYD": "Libyan dinar",
    "MAD": "Moroccan dirham",
    "MDL": "Moldovan leu",
    "MGA": "Malagasy ariary",
    "MKD": "Macedonian denar",
    "MMK": "Myanmar kyat",
    "MNT": "Mongolian togrog",
    "MOP": "Macanese pataca",
    "MRU": "Mauritanian ouguiya",
    "MUR": "Mauritian rupee",
    "MVR": "Maldivian rufiyaa",
    "MWK": "Malawian kwacha",
    "MXN": "Mexican peso",
    "MYR": "Malaysian ringgit",
    "MZN": "Mozambican metical",
    "NAD": "Namibian dollar",
    "NGN": "Nigerian naira",
    "NIO": "Nicaraguan cordoba",
    "NOK": "Norwegian krone",
    "NPR": "Nepalese rupee",
    "NZD": "New Zealand dollar",
    "OMR": "Omani rial",
    "PAB": "Panamanian balboa",
    "PEN": "Peruvian sol",
    "PGK": "Papua New Guinean kina",
    "PHP": "Philippine peso",
    "PKR": "Pakistani rupee",
    "PLN": "Polish zloty",
    "PYG": "Paraguayan guarani",
    "QAR": "Qatari riyal",
    "RON": "Romanian leu",
    "RSD": "Serbian dinar",
    "RUB": "Russian ruble",
    "RWF": "Rwandan franc",
    "SAR": "Saudi riyal",
    "SBD": "Solomon Islands dollar",
    "SCR": "Seychellois rupee",
    "SDG": "Sudanese pound",
    "SEK": "Swedish krona",
    "SGD": "Singapore dollar",
    "SHP": "Saint Helena pound",
    "SLE": "Sierra Leonean leone",
    "SLL": "Sierra Leonean leone",
    "SOS": "Somali shilling",
    "SRD": "Surinamese dollar",
    "SSP": "South Sudanese pound",
    "STN": "Sao Tome and Principe dobra",
    "SYP": "Syrian pound",
    "SZL": "Swazi lilangeni",
    "THB": "Thai baht",
    "TJS": "Tajikistani somoni",
    "TMT": "Turkmenistan manat",
    "TND": "Tunisian dinar",
    "TOP": "Tongan paanga",
    "TRY": "Turkish lira",
    "TTD": "Trinidad and Tobago dollar",
    "TWD": "New Taiwan dollar",
    "TZS": "Tanzanian shilling",
    "UAH": "Ukrainian hryvnia",
    "UGX": "Ugandan shilling",
    "USD": "US dollar",
    "UYU": "Uruguayan peso",
    "UZS": "Uzbekistani som",
    "VES": "Venezuelan bolivar",
    "VND": "Vietnamese dong",
    "VUV": "Vanuatu vatu",
    "WST": "Samoan tala",
    "XAF": "Central African CFA franc",
    "XCD": "East Caribbean dollar",
    "XOF": "West African CFA franc",
    "XPF": "CFP franc",
    "YER": "Yemeni rial",
    "ZAR": "South African rand",
    "ZMW": "Zambian kwacha",
    "ZWL": "Zimbabwean dollar",
}


def currency_name(code: str) -> str:
    normalized = code.upper()
    return CURRENCY_NAMES.get(normalized, normalized)


def currency_label(code: str) -> str:
    normalized = code.upper()
    name = currency_name(normalized)
    return f"{normalized} - {name}" if name != normalized else normalized


def parse_amount(raw_value: str) -> Decimal:
    cleaned = raw_value.strip().replace(" ", "").replace(",", ".")
    if not cleaned:
        raise ValueError("amount_required")
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError("invalid_number") from exc
    if not value.is_finite():
        raise ValueError("invalid_number")
    if value < 0:
        raise ValueError("negative_amount")
    return value


def format_amount(value: Decimal, places: str = "0.01") -> str:
    quantizer = Decimal(places)
    whole_digits = max(value.adjusted() + 1, 1)
    fractional_digits = max(-quantizer.as_tuple().exponent, 0)
    significant_digits = len(value.as_tuple().digits)
    precision = max(28, whole_digits + fractional_digits + 2, significant_digits + 2)
    with localcontext() as context:
        context.prec = precision
        quantized = value.quantize(quantizer, rounding=ROUND_HALF_UP)
    text = format(quantized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def group_integer_digits(value: str) -> str:
    sign = ""
    digits = value
    if digits.startswith(("+", "-")):
        sign = "-" if digits[0] == "-" else ""
        digits = digits[1:]
    digits = digits.lstrip("0") or "0"
    groups: list[str] = []
    while digits:
        groups.append(digits[-3:])
        digits = digits[:-3]
    return sign + " ".join(reversed(groups))


def format_amount_grouped(
    value: Decimal,
    places: str = "0.01",
    decimal_separator: str = ".",
) -> str:
    text = format_amount(value, places)
    if "." not in text:
        return group_integer_digits(text)
    integer_part, fractional_part = text.split(".", 1)
    separator = "," if decimal_separator == "," else "."
    return f"{group_integer_digits(integer_part)}{separator}{fractional_part}"


def format_input_amount_grouped(raw_value: str) -> str:
    parse_amount(raw_value)
    cleaned = raw_value.strip().replace(" ", "")
    if not cleaned:
        return cleaned
    separator = "," if "," in cleaned and "." not in cleaned else "."
    parts = cleaned.split(separator, 1)
    integer_part = parts[0]
    fractional_part = parts[1] if len(parts) > 1 else ""
    if integer_part.startswith("+"):
        integer_part = integer_part[1:]
    if not integer_part:
        integer_part = "0"
    if not integer_part.lstrip("-").isdigit() or (
        len(parts) > 1 and fractional_part and not fractional_part.isdigit()
    ):
        return format_amount_grouped(parse_amount(raw_value), decimal_separator=separator)
    grouped = group_integer_digits(integer_part)
    if len(parts) == 1:
        return grouped
    return f"{grouped}{separator}{fractional_part}"


def convert_amount(amount: Decimal, rate: float) -> Decimal:
    return amount * Decimal(str(rate))


class RatesProvider:
    def __init__(
        self,
        session: Any | None = None,
        base_url: str = "https://free.ratesdb.com/v1/rates",
        timeout: int = 5,
    ) -> None:
        self._session = session
        self._base_url = base_url
        self._timeout = timeout

    @property
    def api_provider_label(self) -> str:
        parsed = urlparse(self._base_url)
        return parsed.netloc or self._base_url

    def fetch_rates(self, base_currency: str) -> RateSnapshot:
        base = base_currency.upper()
        payload = self._fetch_payload(base)
        data = payload["data"]
        rates = {
            str(code).upper(): float(value)
            for code, value in dict(data["rates"]).items()
        }
        rates[base] = 1.0
        return RateSnapshot(
            base=base,
            date=str(data["date"]),
            rates=rates,
            source="online",
        )

    def _fetch_payload(self, base_currency: str) -> dict[str, Any]:
        if self._session is not None:
            response = self._session.get(
                self._base_url,
                params={"from": base_currency},
                timeout=self._timeout,
            )
            response.raise_for_status()
            return response.json()

        query = urlencode({"from": base_currency})
        request = Request(
            f"{self._base_url}?{query}",
            headers={
                "Accept": "application/json",
                "User-Agent": f"{APP_NAME}/{APP_VERSION}",
            },
        )
        with urlopen(request, timeout=self._timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return json.loads(response.read().decode(charset))


class RatesCache:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load_raw(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "bases": {}}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"version": 1, "bases": {}}

    def save_snapshot(self, snapshot: RateSnapshot) -> None:
        payload = self.load_raw()
        payload.setdefault("version", 1)
        payload.setdefault("bases", {})
        snapshot_payload = snapshot.to_cache_record()
        if not snapshot_payload.get("cached_at"):
            snapshot_payload["cached_at"] = datetime.now(timezone.utc).isoformat()
        payload["bases"][snapshot.base] = snapshot_payload
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            return

    def get_snapshot(self, base_currency: str) -> RateSnapshot | None:
        payload = self.load_raw()
        base = base_currency.upper()
        record = dict(payload.get("bases", {})).get(base)
        if not isinstance(record, dict):
            return None
        return RateSnapshot.from_cache_record(base, record)

    def all_snapshots(self) -> dict[str, RateSnapshot]:
        payload = self.load_raw()
        snapshots: dict[str, RateSnapshot] = {}
        for base, record in dict(payload.get("bases", {})).items():
            if isinstance(record, dict):
                snapshots[str(base).upper()] = RateSnapshot.from_cache_record(
                    str(base).upper(),
                    record,
                )
        return snapshots

    def available_currencies(self) -> list[str]:
        codes: set[str] = set()
        for base, snapshot in self.all_snapshots().items():
            codes.add(base)
            codes.update(snapshot.rates.keys())
        return sorted(codes) if codes else list(DEFAULT_CURRENCIES)

    def is_empty(self) -> bool:
        return not bool(self.load_raw().get("bases"))

    def seed_from_legacy_file(self, legacy_path: Path = LEGACY_CACHE_FILE) -> bool:
        if not self.is_empty() or not legacy_path.exists():
            return False
        try:
            payload = json.loads(legacy_path.read_text(encoding="utf-8"))
            data = payload["data"]
            base = str(data["from"]).upper()
            snapshot = RateSnapshot(
                base=base,
                date=str(data["date"]),
                rates={
                    str(code).upper(): float(value)
                    for code, value in dict(data["rates"]).items()
                },
                source="cache",
            )
            snapshot.rates[base] = 1.0
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
            return False
        self.save_snapshot(snapshot)
        return True


class PreferencesStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> AppPreferences:
        if not self.path.exists():
            return AppPreferences()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return AppPreferences()
        return AppPreferences.from_dict(payload)

    def save(self, preferences: AppPreferences) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(preferences.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            return


def rate_for(snapshot: RateSnapshot | None, target_currency: str) -> float | None:
    if snapshot is None:
        return None
    return snapshot.rates.get(target_currency.upper())


__all__ = [
    "APP_NAME",
    "APP_VERSION",
    "AUTHOR",
    "GITHUB",
    "DEFAULT_LANGUAGE",
    "FALLBACK_LANGUAGE",
    "LANGUAGE_CODES",
    "LANGUAGE_SHORT_LABELS",
    "DEFAULT_CURRENCIES",
    "AppPreferences",
    "LocaleManager",
    "PreferencesStore",
    "RateSnapshot",
    "RatesCache",
    "RatesProvider",
    "convert_amount",
    "currency_label",
    "currency_name",
    "format_amount",
    "format_amount_grouped",
    "format_input_amount_grouped",
    "parse_amount",
    "rate_for",
]
