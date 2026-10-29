"""All sources, by the name used in config/settings.yaml."""
from mpf.sources.dfo_far import DFOFARSource
from mpf.sources.iaac import IAACSource
from mpf.sources.nl_ea import NLEASource

# Web sources. The drop folder is handled separately because it reads local files.
SOURCES = {
    "iaac": IAACSource,
    "nl_ea": NLEASource,
    "dfo_far": DFOFARSource,
}
