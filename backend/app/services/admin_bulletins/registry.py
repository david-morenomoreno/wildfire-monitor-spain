from app.services.admin_bulletins.base import AdminBulletinSource
from app.services.admin_bulletins.jcyl import JcylBulletinSource

# Add one entry per region as new AdminBulletinSource implementations are built.
REGION_SOURCES: dict[str, AdminBulletinSource] = {
    "jcyl": JcylBulletinSource(),
}
