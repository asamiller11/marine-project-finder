"""All sources, by the name used in config/settings.yaml."""
from mpf.sources.bc_eao import BCEAOSource
from mpf.sources.ca_coastal import CACoastalSource
from mpf.sources.ceqanet import CEQAnetSource
from mpf.sources.dfo_far import DFOFARSource
from mpf.sources.dk_hoering import DKHoeringSource
from mpf.sources.es_boe import ESBOESource
from mpf.sources.fed_register import FedRegisterSource
from mpf.sources.fed_register_broad import FedRegisterBroadSource
from mpf.sources.fr_eolien import FREolienSource
from mpf.sources.fr_mce import FRMCESource
from mpf.sources.iaac import IAACSource
from mpf.sources.nl_ea import NLEASource
from mpf.sources.nl_sru import NLSRUSource
from mpf.sources.noaa_ita import NOAAITASource
from mpf.sources.ns_ea import NSEASource
from mpf.sources.qc_ree import QCREESource
from mpf.sources.scot_marine import ScotMarineSource

# Web sources. The drop folder is handled separately because it reads local files.
SOURCES = {
    "iaac": IAACSource,
    "nl_ea": NLEASource,
    "bc_eao": BCEAOSource,
    "dk_hoering": DKHoeringSource,
    "scot_marine": ScotMarineSource,
    "es_boe": ESBOESource,
    "fr_eolien": FREolienSource,
    "dfo_far": DFOFARSource,
    # US Federal Register (NOAA incidental harassment notices), shown under the hard-structure profile only.
    "fed_register": FedRegisterSource,
    # Batch 3 sources (2026-11-17), all OFF until the owner switches them on.
    "noaa_ita": NOAAITASource,
    "fed_register_broad": FedRegisterBroadSource,
    "fr_mce": FRMCESource,
    "nl_sru": NLSRUSource,
    # California Coastal Commission agendas (written permission, 2026-11-19).
    "ca_coastal": CACoastalSource,
    # Sources whose owners gave written permission in reply to the 2026-11-17 requests (owner, 2026-11-23).
    "ns_ea": NSEASource,
    "ceqanet": CEQAnetSource,
    "qc_ree": QCREESource,
}

# Live sources the scheduled GitHub run reads (the DFO test set and drop folder are local only).
LIVE_SOURCES = ["iaac", "nl_ea", "bc_eao", "dk_hoering", "scot_marine", "es_boe", "fr_eolien", "fed_register",
                "noaa_ita", "fed_register_broad", "fr_mce", "nl_sru", "ca_coastal", "ns_ea", "ceqanet", "qc_ree"]

# A page each source must be able to reach, for `python -m mpf check-access`.
CHECK_URLS = {
    "iaac": "https://iaac-aeic.gc.ca/050/evaluations/participation/comment?culture=en-CA",
    "nl_ea": "https://www.gov.nl.ca/eccc/env-assessment/projects-list/",
    "bc_eao": "https://projects.eao.gov.bc.ca/api/public/search?dataset=CommentPeriod&pageSize=1&pageNum=0",
    "dk_hoering": "https://hoeringsportalen.dk/Syndication/HearingsFeed",
    "scot_marine": "https://marine.gov.scot/data/marine-licence-documentation",
    "es_boe": "https://www.boe.es/datosabiertos/api/boe/sumario/{date}",
    "fr_eolien": "https://www.eoliennesenmer.fr/parcs-et-projets-eoliens-en-france",
    "fed_register": "https://www.federalregister.gov/api/v1/documents.json?per_page=1",
    "ns_ea": "https://novascotia.ca/nse/ea/projects.asp",
    "ceqanet": "https://ceqanet.lci.ca.gov/Search/Recent",
    "qc_ree": "https://www.ree.environnement.gouv.qc.ca/index.asp",
}
