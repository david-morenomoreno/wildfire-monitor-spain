from fastapi import APIRouter, HTTPException, Query

from app.services.fire_spread import predict_spread

router = APIRouter(prefix="/api/fire-spread", tags=["fire-spread"])


@router.get("/predict")
def predict(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    max_hours: int = Query(24, ge=1, le=24),
):
    """
    Experimental: given a fire origin point, estimates the affected-area
    ellipse for each hour of the Open-Meteo wind forecast (up to 24h), using
    a Corine-derived fuel guess and local slope. See
    app/services/fire_spread.py's module docstring for the model and its
    limitations - this is a POC, not an operational tool.
    """
    try:
        return predict_spread(lat, lon, max_hours=max_hours)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Fire spread prediction failed: {exc}") from exc
