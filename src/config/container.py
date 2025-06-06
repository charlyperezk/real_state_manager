import asyncio
import copy
import inspect
import json
import uuid
from typing import Optional
from uuid import UUID

from dependency_injector import containers, providers
from dependency_injector.containers import Container
from dependency_injector.providers import Dependency, Factory, Provider, Singleton
from dependency_injector.wiring import Provide, inject  # noqa
from lato import Application, DependencyProvider, TransactionContext
from pydantic_settings import BaseSettings
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ..modules.strategy.application import strategy_module
from ..modules.strategy.infrastructure.strategy_postgres_repository import (
    StrategyPostgresJsonManagementRepository
)

from ..modules.partner.application import partner_module
from ..modules.partner.application.services.partner_fees_provider import (
    PartnerFeesProvider,
    PartnerEvaluator
)
from ..modules.partner.infrastructure.partner_postgres_repository import (
    PartnerPostgresJsonManagementRepository
)

from ..modules.operation.application import operation_module
from ..modules.operation.infrastructure.operation_postgres_repository import (
    OperationPostgresJsonManagementRepository
)

from src.seedwork.application.inbox_outbox import InMemoryOutbox
from src.seedwork.infrastructure.logging import Logger, logger


def _default(val):
    import uuid
    import datetime

    if isinstance(val, datetime.datetime):
        return val.isoformat()
    if hasattr(val, "__dict__"):
        return val.__dict__
    if isinstance(val, uuid.UUID):
        return str(val)
    raise TypeError()


def dumps(d):
    return json.dumps(d, default=_default)


def create_db_engine(config):
    engine = create_engine(
        config.DATABASE_URL, echo=config.DATABASE_ECHO, json_serializer=dumps
    )
    from src.seedwork.infrastructure.database import Base

    # TODO: it seems like a hack, but it works...
    Base.metadata.create_all(engine)
    Base.metadata.bind = engine
    return engine


def create_application(db_engine) -> Application:
    """Creates new instance of the application"""
    application = Application(
        "RealEstateManagerApp",
        app_version=0.1,
        db_engine=db_engine,
    )

    application.include_submodule(strategy_module)
    application.include_submodule(partner_module)
    application.include_submodule(operation_module)

    @application.on_create_transaction_context
    def on_create_transaction_context(**kwargs):
        engine = application.get_dependency("db_engine")
        session = Session(engine)
        correlation_id = uuid.uuid4()
        logger.correlation_id.set(uuid.uuid4())  # type: ignore
        strategy_repository=StrategyPostgresJsonManagementRepository(db_session=session)

        # create IoC container for the transaction
        dependency_provider = ContainerProvider(
            TransactionContainer(
                db_session=session,
                correlation_id=correlation_id, 
                logger=logger,
                strategy_repository=strategy_repository
            )
        )

        return TransactionContext(dependency_provider)

    @application.on_enter_transaction_context
    def on_enter_transaction_context(ctx: TransactionContext):
        ctx.set_dependencies(publish=ctx.publish)
        logger.debug("Entering transaction")

    @application.on_exit_transaction_context
    def on_exit_transaction_context(
        ctx: TransactionContext, exception: Optional[Exception] = None
    ):
        session = ctx["db_session"]
        if exception:
            session.rollback()
            logger.warning(f"rollback due to {exception}")

            # from pydantic import ValidationError
            # if type(exception) not in [ValidationError]:
            #     raise exception
        else:
            session.commit()
            logger.debug(f"committed")
        session.close()
        logger.debug(f"transaction ended")
        logger.correlation_id.set(uuid.UUID(int=0))  # type: ignore

    @application.transaction_middleware
    async def logging_middleware(ctx: TransactionContext, call_next):
        description = (
            f"{ctx.current_action[1]} -> {repr(ctx.current_action[0])}"
            if ctx.current_action
            else ""
        )
        logger.debug(f"Executing {description}...")
        result = call_next()
        if asyncio.iscoroutine(result):
            result = await result
        logger.debug(f"Finished executing {description}")
        return result

    @application.transaction_middleware
    async def event_collector_middleware(ctx: TransactionContext, call_next):
        handler_kwargs = call_next.keywords

        result = call_next()
        if asyncio.iscoroutine(result):
            result = await result

        logger.debug(f"Collecting event from {ctx['message'].__class__}")

        domain_events = []
        repositories = filter(
            lambda x: hasattr(x, "collect_events"), handler_kwargs.values()
        )
        for repo in repositories:
            domain_events.extend(repo.collect_events())
        for event in domain_events:
            logger.debug(f"Publishing {event}")
            await ctx.publish_async(event)

        return result

    return application


class ApplicationContainer(containers.DeclarativeContainer):
    """Dependency Injection container for the application (application-level dependencies)
    see https://github.com/ets-labs/python-dependency-injector for more details
    """

    __self__ = providers.Self()
    config = providers.Dependency(instance_of=BaseSettings)
    db_engine = providers.Singleton(create_db_engine, config)
    application = providers.Singleton(create_application, db_engine)


class TransactionContainer(containers.DeclarativeContainer):
    """Dependency Injection container for the transaction context (transaction-level dependencies)
    Most of the dependencies are singletons, as each transaction receives new transaction container.
    """

    correlation_id = providers.Dependency(instance_of=UUID)
    db_session = providers.Dependency(instance_of=Session)
    logger = providers.Dependency(instance_of=Logger)

    outbox = providers.Singleton(InMemoryOutbox)

    strategy_repository = providers.Singleton(
        StrategyPostgresJsonManagementRepository,
        db_session=db_session,
    )

    partner_repository = providers.Singleton(
        PartnerPostgresJsonManagementRepository,
        db_session=db_session,
    )

    partner_fees_provider = PartnerFeesProvider(
        repository=partner_repository, # type: ignore
        evaluator=PartnerEvaluator()
    )

    operation_repository = providers.Singleton(
        OperationPostgresJsonManagementRepository,
        db_session=db_session,
    )

def resolve_provider_by_type(container: Container, cls: type) -> Optional[Provider]:
    def inspect_provider(provider: Provider) -> bool:
        if isinstance(provider, (Factory, Singleton)):
            return issubclass(provider.cls, cls)
        elif isinstance(provider, Dependency):
            return issubclass(provider.instance_of, cls)

        return False

    matching_providers = inspect.getmembers(
        container,
        inspect_provider,
    )
    if matching_providers:
        if len(matching_providers) > 1:
            raise ValueError(
                f"Cannot uniquely resolve {cls}. Found {len(providers)} matching resources."
            )
        return matching_providers[0][1]
    return None


class ContainerProvider(DependencyProvider):
    """A dependency provider that uses a dependency injector container under the hood"""

    def __init__(self, container: Container):
        self.container = container
        self.counter = 0

    def has_dependency(self, identifier: str | type) -> bool:
        if isinstance(identifier, type) and resolve_provider_by_type(
            self.container, identifier
        ):
            return True
        if type(identifier) is str:
            return identifier in self.container.providers
        return False

    def register_dependency(self, identifier, dependency_instance):
        pr = providers.Object(dependency_instance)
        try:
            setattr(self.container, identifier, pr)
        except TypeError:
            setattr(self.container, f"{str(identifier)}-{self.counter}", pr)
            self.counter += 1

    def get_dependency(self, identifier):
        try:
            if isinstance(identifier, type):
                provider = resolve_provider_by_type(self.container, identifier)
            else:
                provider = getattr(self.container, identifier)
            instance = provider()
        except Exception as e:
            raise e
        return instance

    def copy(self, *args, **kwargs):
        dp = ContainerProvider(copy.copy(self.container))
        dp.update(*args, **kwargs)
        return dp
