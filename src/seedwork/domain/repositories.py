import abc
from typing import Generic, TypeVar

from src.seedwork.domain.entities import Entity as DomainEntity
from src.seedwork.domain.value_objects import GenericUUID

Entity = TypeVar("Entity", bound=DomainEntity)
EntityId = TypeVar("EntityId", bound=GenericUUID)


class GenericRepository(Generic[EntityId, Entity], metaclass=abc.ABCMeta):
    """An interface for a generic repository"""

    @abc.abstractmethod
    def add(self, entity: Entity) -> None:
        raise NotImplementedError()

    @abc.abstractmethod
    def remove(self, entity: Entity) -> None:
        raise NotImplementedError()

    @abc.abstractmethod
    def get_by_id(self, entity_id: EntityId) -> Entity:
        raise NotImplementedError()

    @abc.abstractmethod
    def persist(self, entity: Entity) -> None:
        raise NotImplementedError()

    @abc.abstractmethod
    def persist_all(self) -> None:
        raise NotImplementedError()

    @abc.abstractmethod
    def collect_events(self):
        raise NotImplementedError()

    def __getitem__(self, index) -> Entity:
        return self.get_by_id(index)

    @staticmethod
    def next_id() -> EntityId:
        return GenericUUID.next_id()
