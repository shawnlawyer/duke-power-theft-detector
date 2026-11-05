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
        slug="coned",
        provider="Con Edison",
        aliases=("ConEd", "Consolidated Edison", "Consolidated Edison Company of New York"),
        title="Download your Con Edison history",
        summary="Con Edison lets you download your energy history, including detailed readings from a smart meter.",
        steps=(
            "Open Con Edison's download page, choose Download Green Button Data, and sign in to My Account.",
            "Choose your electric account and dates. Download the smart-meter readings as Green Button XML, rather than a bill or a summary of charges.",
            "Return to this account's History page and upload the file. If it is a ZIP, extract the electricity file first. CSV files must match the interval format listed on History.",
        ),
        url="https://www.coned.com/en/save-money/make-better-energychoices-with-green-button",
        link_label="Open Con Edison download instructions",
        note="Choose electricity, not gas or steam. These instructions are for Con Edison in New York, not ComEd in Illinois. Keep your password and verification codes on Con Edison's site.",
        source_url="https://www.coned.com/en/save-money/make-better-energychoices-with-green-button",
    ),
    UtilityDownloadGuide(
        slug="orange-rockland",
        provider="Orange & Rockland",
        aliases=("Orange and Rockland Utilities", "O&R", "ORU"),
        title="Download your Orange & Rockland history",
        summary="Use Orange & Rockland's Green Button download to bring your electricity history here.",
        steps=(
            "Open Orange & Rockland's download page, choose Download Green Button Data, and sign in to your account.",
            "Select your electric account and dates. Choose hourly or shorter meter readings when available, rather than monthly usage totals.",
            "Return to this account's History page with Green Button XML or a CSV that matches the listed interval format. Extract the electricity file first if your download is a ZIP.",
        ),
        url="https://www.oru.com/en/save-money/green-button",
        link_label="Open Orange & Rockland download instructions",
        note="The detail available depends on your meter and export. Monthly totals cannot show overnight usage. A file upload here does not enroll you in Share My Data.",
        source_url="https://www.oru.com/en/save-money/green-button",
    ),
    UtilityDownloadGuide(
        slug="national-grid",
        provider="National Grid",
        aliases=("National Grid USA", "National Grid USA Service Company"),
        title="Get your National Grid electricity history",
        summary="National Grid offers a way to access your own data and download files. Start with its customer instructions.",
        steps=(
            "Open National Grid's Green Button page and select your service region. Find Access your own data, then follow its setup instructions.",
            "In the data dashboard, choose your electric account and download hourly or shorter readings as Green Button XML. Keep gas usage separate.",
            "Return to this account's History page and upload the electricity file. If you downloaded CSV instead, check it against the interval format listed on History.",
        ),
        url="https://www.nationalgridus.com/MA-Home/Ways-to-Save/Green-Button-Connect",
        link_label="Open National Grid data instructions",
        note="Accessing your own data may require a separate setup. This is not an automatic National Grid connection to Home Energy Watch. An account with only gas service cannot provide electricity readings.",
        source_url="https://www.nationalgridus.com/MA-Home/Ways-to-Save/Green-Button-Connect",
    ),
    UtilityDownloadGuide(
        slug="eversource-ma",
        provider="Eversource Massachusetts",
        aliases=("Eversource MA", "Eversource Energy Massachusetts", "Eversource Energy - Massachusetts"),
        title="Find your Eversource Massachusetts history",
        summary="Eversource's Green Button page lets you download electricity usage history. Check whether the file includes the detail you need.",
        steps=(
            "Open Eversource's Green Button page and choose your Massachusetts town if asked. Have your account number and service ZIP code ready.",
            "Use the Green Button to download history for your electric account. Check for hourly or shorter readings, rather than just a summary of billing periods.",
            "If the file contains interval readings, return to this account's History page and upload Green Button XML or a CSV that matches the listed interval format.",
        ),
        url="https://www.eversource.com/residential/save-money-energy/energy-savings-tips-tools/green-button",
        link_label="Open Eversource download instructions",
        note="Eversource describes a 13-month usage summary; that does not guarantee hourly readings in every download. These instructions are for Massachusetts. UsageLink's automatic sharing is a separate service, not a connection enabled here.",
        source_url="https://www.eversource.com/residential/save-money-energy/energy-savings-tips-tools/green-button",
    ),
    UtilityDownloadGuide(
        slug="swepco",
        provider="SWEPCO",
        aliases=("Southwestern Electric Power", "Southwestern Electric Power Company"),
        title="Download your SWEPCO history",
        summary="SWEPCO has a Green Button download for your usage history. Bring the file here to review the readings it contains.",
        steps=(
            "Open SWEPCO's Green Button page and choose Green Button download, rather than Green Button view. Sign in when asked.",
            "Choose your electric account and dates. Check that the download contains hourly or shorter readings, not just monthly totals.",
            "Return to this account's History page and upload Green Button XML or a CSV that matches the listed interval format. Extract the electricity file first if the download is a ZIP.",
        ),
        url="https://www.swepco.com/account/usage/green-button/",
        link_label="Open SWEPCO download instructions",
        note="Available history and detail depend on your account and meter. A download does not set up automatic updates here. Keep your SWEPCO password and verification codes on its own site.",
        source_url="https://www.swepco.com/account/usage/green-button/",
    ),
    UtilityDownloadGuide(
        slug="sdge",
        provider="San Diego Gas & Electric",
        aliases=("SDG&E", "SDGE"),
        title="Download your SDG&E history",
        summary="SDG&E offers up to 13 months of smart-meter history through My Energy Center.",
        steps=(
            "Sign in to My Energy Center and choose the electric account you want to review.",
            "Open Green Button Download My Data. Choose your dates and the electricity usage file, with hourly or 15-minute readings.",
            "Return to this account's History page and upload Green Button XML or a CSV that matches the listed interval format. If your download is a ZIP, extract the electricity file first.",
        ),
        url="https://www.sdge.com/green-button",
        link_label="Open SDG&E download instructions",
        note="Choose electricity readings, not gas usage or bill totals. You do not need to share your My Energy Center password with Home Energy Watch.",
        source_url="https://www.sdge.com/green-button",
    ),
    UtilityDownloadGuide(
        slug="sce",
        provider="Southern California Edison",
        aliases=("SCE",),
        title="Download your Southern California Edison history",
        summary="Use SCE My Account to download your electricity readings and bring the file here.",
        steps=(
            "Sign in to SCE My Account. Complete any verification SCE asks for on its own site.",
            "Choose Data Sharing & Download, then Green Button Download My Data. Select the electric service account and dates you want to review.",
            "Return to the matching account's History page with Green Button XML or a CSV that matches the listed interval format. If your download is a ZIP, extract the electricity file first.",
        ),
        url="https://www.sce.com/",
        link_label="Open SCE",
        note="Keep your SCE password and verification codes on SCE's site. This upload does not create an automatic SCE connection or require its separate paid data-request service.",
        source_url="https://www.sce.com/privacy-notice/california-consumer-privacy-act-policy/personal-information-request",
    ),
    UtilityDownloadGuide(
        slug="pge",
        provider="Pacific Gas & Electric",
        aliases=("PG&E",),
        title="Download your PG&E history",
        summary="PG&E lets you download your usage data at no charge, one service agreement at a time.",
        steps=(
            "Sign in to PG&E. Under Usage and rates, find Bill period usage and select View Usage Details.",
            "Choose Green Button Download my data, then the electric service agreement and date range. Use interval readings rather than bill totals.",
            "Return to the matching account's History page with Green Button XML or a CSV that matches the listed interval format. If your download is a ZIP, extract the electricity file first.",
        ),
        url="https://www.pge.com/en/save-energy-and-money/energy-usage-and-tips/understand-my-usage.html",
        link_label="Open PG&E download instructions",
        note="These instructions are for Pacific Gas & Electric in California, not Portland General Electric. Downloading a file does not enroll you in Share My Data.",
        source_url="https://www.pge.com/en/save-energy-and-money/energy-usage-and-tips/understand-my-usage/energy-data-hub.html",
    ),
    UtilityDownloadGuide(
        slug="central-hudson",
        provider="Central Hudson",
        aliases=("Central Hudson Gas & Electric",),
        title="Find your Central Hudson usage history",
        summary="Start with My Usage History to see the electricity records available for your account.",
        steps=(
            "Sign in to Central Hudson and open My Usage History for your electric account.",
            "Look for a usage download. If none is available, ask Central Hudson whether it can provide hourly or shorter electricity readings at no charge.",
            "If you receive Green Button XML or a CSV with interval readings, return to this account's History page and check it against the supported formats before uploading.",
        ),
        url="https://www.cenhud.com/en/myaccount/usage/",
        link_label="Open Central Hudson usage history",
        note="An hourly download is not confirmed for every account. Daily or monthly totals cannot show overnight usage. Business accounts may have a separate Energy Manager or EDI route; ask about eligibility and fees before requesting it.",
        source_url="https://www.cenhud.com/en/esco/",
    ),
    UtilityDownloadGuide(
        slug="delmarva",
        provider="Delmarva Power",
        aliases=("Delmarva Power & Light",),
        title="Find your Delmarva Power download",
        summary="Delmarva's My Usage page links to My Green Button Data for your account.",
        steps=(
            "Open Delmarva's My Usage page, choose My Green Button Data, and sign in to your account.",
            "Select your electric account and download the available usage file. Check that it contains hourly or shorter readings, not just daily or monthly totals.",
            "Return to this account's History page and check the supported formats. Use Green Button XML or a matching interval CSV; if the file is zipped, extract it first.",
        ),
        url="https://www.delmarva.com/my-account/my-dashboard/my-usage",
        link_label="Open Delmarva My Usage",
        note="The download format and level of detail need to be checked for your account. You do not need to register as an energy supplier or broker to try the customer download.",
        source_url="https://www.delmarva.com/my-account/my-dashboard/my-usage",
    ),
    UtilityDownloadGuide(
        slug="atlantic-city-electric",
        provider="Atlantic City Electric",
        aliases=(),
        title="Find your Atlantic City Electric download",
        summary="Atlantic City Electric's My Usage page links to My Green Button Data, where you can access your usage history.",
        steps=(
            "Open Atlantic City Electric's My Usage page, choose My Green Button Data, and sign in on the utility's site.",
            "Choose your electric account and download the available usage file. Look for hourly or shorter readings, rather than daily totals or a bill PDF.",
            "Return to this account's History page with Green Button XML or a CSV that matches the listed interval format. If your download is a ZIP, extract the electricity file first.",
        ),
        url="https://www.atlanticcityelectric.com/my-account/my-dashboard/my-usage",
        link_label="Open Atlantic City Electric My Usage",
        note="Available detail depends on your account and meter. This is a customer download, not supplier registration or an automatic connection. If you are asked to pay for a separate data request, stop before ordering it.",
        source_url="https://www.atlanticcityelectric.com/my-account/my-dashboard/my-usage",
    ),
    UtilityDownloadGuide(
        slug="pepco",
        provider="Pepco",
        aliases=("Potomac Electric Power",),
        title="Find your Pepco download",
        summary="Pepco's My Usage page gives you access to My Green Button Data for your electricity account.",
        steps=(
            "Open Pepco's My Usage page, choose My Green Button Data, and sign in on Pepco's site.",
            "Choose your electric account and download the available usage file. Check for hourly or shorter readings, not just daily or monthly totals.",
            "Return to this account's History page and upload Green Button XML or a CSV that matches the listed interval format. Extract the electricity file first if it arrives in a ZIP.",
        ),
        url="https://www.pepco.com/my-account/my-dashboard/my-usage",
        link_label="Open Pepco My Usage",
        note="These instructions are for Pepco, not PECO. Keep your password and verification codes on Pepco's site. Use the customer download; stop if a separate data request asks for payment.",
        source_url="https://www.pepco.com/my-account/my-dashboard/my-usage",
    ),
    UtilityDownloadGuide(
        slug="peco",
        provider="PECO",
        aliases=("PECO Energy",),
        title="Find your PECO electricity download",
        summary="PECO's My Green Button Data lets you access electricity usage to review outside your utility account.",
        steps=(
            "Open PECO's My Usage page, choose My Green Button Data, and sign in on PECO's site.",
            "Choose your electric account, not gas service, and download the available usage file. Check for hourly or shorter readings instead of billing totals.",
            "Return to the matching account's History page with Green Button XML or a CSV that matches the listed interval format. Extract the electricity file first if the download is a ZIP.",
        ),
        url="https://www.peco.com/my-account/my-dashboard/my-usage",
        link_label="Open PECO My Usage",
        note="PECO and Pepco are different utilities. This upload does not create an automatic connection. Use the customer download; stop if a separate data request asks for payment.",
        source_url="https://www.peco.com/my-account/my-dashboard/my-usage",
    ),
    UtilityDownloadGuide(
        slug="comed",
        provider="ComEd",
        aliases=("Commonwealth Edison",),
        title="Find your ComEd download",
        summary="ComEd's My Usage page links to My Green Button Data for your electricity history.",
        steps=(
            "Open ComEd's My Usage page, choose My Green Button Data, and sign in on ComEd's site.",
            "Choose your electric account and download the available usage file. Check for hourly or shorter readings rather than daily or monthly totals.",
            "Return to this account's History page with Green Button XML or a CSV that matches the listed interval format. If the download is a ZIP, extract the electricity file first.",
        ),
        url="https://www.comed.com/my-account/my-dashboard/my-usage",
        link_label="Open ComEd My Usage",
        note="ComEd in Illinois is not Con Edison in New York. If your business account cannot display usage, follow ComEd's business guidance on My Usage. This is a file upload, not an automatic connection; stop if a separate data request asks for payment.",
        source_url="https://www.comed.com/my-account/my-dashboard/my-usage",
    ),
    UtilityDownloadGuide(
        slug="hawaiian-electric",
        provider="Hawaiian Electric",
        aliases=("HECO",),
        title="Download your Hawaiian Electric history",
        summary="My Energy Use lets you download electricity readings from your advanced meter.",
        steps=(
            "Sign in to Hawaiian Electric's Online Customer Service Center. From Account Overview, open My Energy Use for the account you want to review.",
            "Choose your dates and download Green Button XML with 15-minute readings. If you choose CSV instead, check that it includes interval readings rather than daily or monthly totals.",
            "Return to the matching account's History page and upload the XML file or a CSV that matches the listed interval format. Keep the original download unchanged.",
        ),
        url="https://www.hawaiianelectric.com/clean-energy-hawaii/grid-modernization-technologies/advanced-meters/my-energy-use-portal",
        link_label="Open Hawaiian Electric download instructions",
        note="Detailed readings require an advanced meter. Large business customers should ask their key account manager about portal access. Keep utility passwords and verification codes on Hawaiian Electric's site; this upload does not enable automatic updates.",
        source_url="https://www.hawaiianelectric.com/clean-energy-hawaii/grid-modernization-technologies/advanced-meters",
        checked_on="2026-10-12",
    ),
    *(
        UtilityDownloadGuide(
            slug=slug,
            provider=provider,
            aliases=aliases,
            title=f"Download your {provider} history",
            summary=f"Use My Meter to download electricity readings for your {provider} account.",
            steps=(
                "Sign in to My Account on the LG&E and KU website. Choose your electric account, then open View My Meter under Account History.",
                "In My Meter, choose Data, then Download. Select electricity, the correct meter, your dates, and 15-minute readings; download the CSV file.",
                "Return to the matching account's History page and check the CSV against the listed interval format before uploading. You can also use a Green Button XML download if available. Keep the original file unchanged.",
            ),
            url="https://lge-ku.com/mymeter-learnmore",
            link_label="Open My Meter download instructions",
            note="My Meter requires an advanced meter. Choose a single electric meter, not gas usage, a group of meters, or monthly totals. Sign in through My Account; you do not need a separate vendor registration for this file upload. Old Dominion Power is not Dominion Energy.",
            source_url="https://lge-ku.com/sites/default/files/media/files/downloads/My-Meter-How-to-download-data.pdf",
            checked_on="2026-10-12",
        )
        for slug, provider, aliases in (
            ("lge", "Louisville Gas & Electric", ("LG&E", "Louisville Gas and Electric")),
            ("kentucky-utilities", "Kentucky Utilities", ("KU",)),
            ("old-dominion-power", "Old Dominion Power", ("ODP",)),
        )
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
