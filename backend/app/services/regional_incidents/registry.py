from app.services.regional_incidents.andalucia import AndaluciaIncidentSource
from app.services.regional_incidents.base import RegionalIncidentSource
from app.services.regional_incidents.castillalamancha import CastillaLaManchaIncidentSource
from app.services.regional_incidents.catalunya import CatalunyaIncidentSource
from app.services.regional_incidents.jcyl import JcylIncidentSource

# Add one entry per region as new live-status feeds are confirmed. Most
# regions surveyed don't have a public one (see README) - only add an entry
# here once a real, verified endpoint exists, not a guessed one.
REGION_SOURCES: dict[str, RegionalIncidentSource] = {
    "jcyl": JcylIncidentSource(),
    "infoca": AndaluciaIncidentSource(),
    "bombers": CatalunyaIncidentSource(),
    "infocam": CastillaLaManchaIncidentSource(),
}
