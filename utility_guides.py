"""Customer download instructions, separate from working feed integrations."""

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class UtilityDownloadGuide:
    slug: str
    provider: str
    aliases: tuple[str, ...]
    title: str
    summary: str
    steps: tuple[str, ...]
    url: str
    link_label: str
    note: str
    source_url: str
    checked_on: str = "2026-10-11"


UTILITY_DOWNLOAD_GUIDES = (
    UtilityDownloadGuide(
        slug="duke",
        provider="Duke Energy",
        aliases=("Duke Energy Progress", "Duke Energy Carolinas", "Duke Energy Ohio", "Duke Energy Indiana", "Duke Energy Kentucky", "Duke Energy Florida"),
        title="Download your Duke history",
        summary="Save your usage history from Duke, then bring the file back here.",
        steps=(
            "Sign in to Duke and open Usage Details for the electric account you want to review.",
            "Use Quick Links to download your usage data. Choose a Green Button XML or interval history file, not a bill PDF.",
            "Return to this account's History page and upload the file. Keep the original file unchanged.",
        ),
        url="https://www.duke-energy.com/my-account/usage?tab=day",
        link_label="Open Duke Usage Details",
        note="You can upload a file without installing the Duke sign-in helper.",
        source_url="https://www.duke-energy.com/my-account/usage?tab=day",
    ),
    UtilityDownloadGuide(
        slug="xcel",
        provider="Xcel Energy",
        aliases=(),
        title="Download your Xcel history",
        summary="Xcel's customer download is available now. Save a Green Button XML file and add it to your history here.",
        steps=(
            "Sign in to Xcel My Account and open the usage or Green Button Download My Data area.",
            "Choose your electric account and date range, then download the Green Button XML file.",
            "Return to this account's History page and upload the file.",
        ),
        url="https://myenergy.xcelenergy.com/",
        link_label="Open Xcel My Account",
        note="This is a file upload, not an automatic Xcel connection.",
        source_url="https://myenergy.xcelenergy.com/greenbutton/green-vendor",
    ),
    UtilityDownloadGuide(
        slug="cmp",
        provider="Central Maine Power",
        aliases=("CMP",),
        title="Download your Central Maine Power history",
        summary="CMP's Download My Data provides hourly readings from your smart meter.",
        steps=(
            "Open CMP's download page, choose Download My Data, and sign in to your account.",
            "Choose your electric account and dates. Select Green Button XML; each file can cover up to 13 months.",
            "Return to this account's History page and upload the original XML file.",
        ),
        url="https://www.cmpco.com/w/download-usage-data",
        link_label="Open CMP download instructions",
        note="Use Download My Data rather than copying a dashboard chart. Keep any missing or estimated-reading markers in the file; estimates are not meter measurements.",
        source_url="https://www.cmpco.com/w/download-usage-data",
    ),
    UtilityDownloadGuide(
        slug="nyseg",
        provider="NYSEG",
        aliases=("New York State Electric & Gas",),
        title="Download your NYSEG history",
        summary="Use Energy Manager to download the electricity readings available for your account.",
        steps=(
            "Sign in to your NYSEG online account and open Energy Manager.",
            "Choose your electric account and dates. Download XML or CSV with hourly readings, rather than a PDF or monthly summary.",
            "Return to this account's History page. Upload Green Button XML or a CSV that matches the listed interval format.",
        ),
        url="https://www.nyseg.com/energy-manager",
        link_label="Open NYSEG Energy Manager instructions",
        note="Hourly electricity readings require a smart meter. New online accounts may need 72 hours before Energy Manager displays data.",
        source_url="https://www.nyseg.com/energy-manager",
    ),
    UtilityDownloadGuide(
        slug="rge",
        provider="RG&E",
        aliases=("RGE", "Rochester Gas & Electric"),
        title="Download your RG&E history",
        summary="Use Energy Manager to download electricity readings for your RG&E account.",
        steps=(
            "Sign in to your RG&E online account and open Energy Manager.",
            "Choose your electric account and dates. Download XML or CSV with hourly readings, rather than a PDF or monthly summary.",
            "Return to this account's History page. Upload Green Button XML or a CSV that matches the listed interval format.",
        ),
        url="https://www.rge.com/en/account/understandyourbill/energy-manager",
        link_label="Open RG&E Energy Manager instructions",
        note="Hourly electricity readings require a smart meter. New online accounts may need 72 hours before Energy Manager displays data.",
        source_url="https://www.rge.com/en/account/understandyourbill/energy-manager",
    ),
    UtilityDownloadGuide(
        slug="pseg-long-island",
        provider="PSEG Long Island",
        aliases=("PSEG LI",),
        title="Download your PSEG Long Island history",
        summary="MySmartEnergy offers free downloads of your smart-meter readings, one account at a time.",
        steps=(
            "Sign in to PSEG Long Island My Account and choose MySmartEnergy from the My Account menu.",
            "Select the electric account, date range, and hourly or 15-minute readings. Download Green Button XML.",
            "Return to the matching account's History page and upload that file. Download other accounts separately.",
        ),
        url="https://www.psegliny.com/myaccount/serviceandrates/mysmartenergy",
        link_label="Open MySmartEnergy instructions",
        note="These instructions are for PSEG Long Island, not PSE&G in New Jersey. Downloading a file does not set up automatic updates here.",
        source_url="https://www.psegliny.com/myaccount/serviceandrates/mysmartenergy",
    ),
    UtilityDownloadGuide(
        slug="united-illuminating",
        provider="United Illuminating",
        aliases=("The United Illuminating", "UI"),
        title="Download your United Illuminating history",
        summary="UI's free Energy Analyzer lets you download electricity usage. Check how much detail is in the file before uploading it here.",
        steps=(
            "Sign in to your United Illuminating online account and open Energy Analyzer.",
            "Download XML or CSV for your electric account and chosen dates, rather than a bill PDF.",
            "Check for hourly or shorter readings. If the file has only daily totals, it cannot show overnight usage. Otherwise, use the formats listed on History.",
        ),
        url="https://www.uinet.com/en/smartenergy/understandyourusage/energy_analyzer",
        link_label="Open UI Energy Analyzer instructions",
        note="UI describes daily usage in Energy Analyzer; hourly downloads are not confirmed. Its separate custom-data service may charge fees and is not needed to try the free download.",
        source_url="https://www.uinet.com/en/smartenergy/understandyourusage/energy_analyzer",
    ),
)


def normalize_provider_name(value: str | None) -> str:
    name = (value or "").casefold().replace("&", " and ")
    words = re.findall(r"[a-z0-9]+", name)
    while words and words[-1] in {"inc", "llc", "corp", "corporation", "company"}:
        words.pop()
    return " ".join(words)


def get_utility_download_guide(provider: str | None) -> UtilityDownloadGuide | None:
    normalized = normalize_provider_name(provider)
    if not normalized:
        return None
    for guide in UTILITY_DOWNLOAD_GUIDES:
        if normalized in {normalize_provider_name(name) for name in (guide.provider, *guide.aliases)}:
            return guide
    return None
