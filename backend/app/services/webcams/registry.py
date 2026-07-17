from app.services.webcams.base import WebcamSource
from app.services.webcams.dgt import DgtWebcamSource

# Add one entry per provider once a real, verified feed exists (Windy's
# Webcams API is a confirmed candidate but needs a registered API key -
# see README - not added here until credentials exist).
WEBCAM_SOURCES: dict[str, WebcamSource] = {
    "dgt": DgtWebcamSource(),
}
