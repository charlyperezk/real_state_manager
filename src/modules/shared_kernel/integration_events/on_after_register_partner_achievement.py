from src.seedwork.domain.value_objects import GenericUUID, Money
from src.seedwork.application.events import IntegrationEvent
from .. import AchievementType, Period

class OnAfterRegisterPartnerAchievement(IntegrationEvent):
    partner_id: GenericUUID
    achievement_type: AchievementType
    revenue: Money
    period: Period