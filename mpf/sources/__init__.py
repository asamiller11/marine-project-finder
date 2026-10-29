"""All sources, by the name used in config/settings.yaml."""
from mpf.sources.bc_eao import BCEAOSource
from mpf.sources.dfo_far import DFOFARSource
from mpf.sources.dk_hoering import DKHoeringSource
from mpf.sources.es_boe import ESBOESource
from mpf.sources.fr_eolien import FREolienSource
from mpf.sources.iaac import IAACSource
from mpf.sources.nl_ea import NLEASource
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
}

# Live sources the scheduled GitHub run reads (the DFO test set and drop folder are local only).
LIVE_SOURCES = ["iaac", "nl_ea", "bc_eao", "dk_hoering", "scot_marine", "es_boe", "fr_eolien"]

# A page each source must be able to reach, for `python -m mpf check-access`.
CHECK_URLS = {
    "iaac": "https://iaac-aeic.gc.ca/050/evaluations/participation/comment?culture=en-CA",
    "nl_ea": "https://www.gov.nl.ca/eccc/env-assessment/projects-list/",
    "bc_eao": "https://projects.eao.gov.bc.ca/api/public/search?dataset=CommentPeriod&pageSize=1&pageNum=0",
    "dk_hoering": "https://hoeringsportalen.dk/Syndication/HearingsFeed",
    "scot_marine": "https://marine.gov.scot/data/marine-licence-documentation",
    "es_boe": "https://www.boe.es/datosabiertos/api/boe/sumario/{date}",
    "fr_eolien": "https://www.eoliennesenmer.fr/parcs-et-projets-eoliens-en-france",
}
