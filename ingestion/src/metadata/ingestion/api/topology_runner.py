#  Copyright 2021 Collate
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#  http://www.apache.org/licenses/LICENSE-2.0
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
"""
Mixin to be used by service sources to dynamically
generate the _run based on their topology.
"""
import traceback
from collections import defaultdict
from functools import singledispatchmethod
from typing import Any, Generic, Iterable, List, Type, TypeVar

from pydantic import BaseModel

from metadata.generated.schema.api.lineage.addLineage import AddLineageRequest
from metadata.generated.schema.entity.data.database import Database
from metadata.generated.schema.entity.data.databaseSchema import DatabaseSchema
from metadata.generated.schema.entity.data.storedProcedure import StoredProcedure
from metadata.ingestion.api.models import Either, Entity
from metadata.ingestion.models.custom_properties import OMetaCustomProperties
from metadata.ingestion.models.ometa_classification import OMetaTagAndClassification
from metadata.ingestion.models.patch_request import PatchRequest
from metadata.ingestion.models.topology import (
    NodeStage,
    ServiceTopology,
    TopologyContext,
    TopologyNode,
    get_topology_node,
    get_topology_root,
)
from metadata.ingestion.ometa.ometa_api import OpenMetadata
from metadata.ingestion.ometa.utils import model_str
from metadata.utils.logger import ingestion_logger
from metadata.utils.source_hash import generate_source_hash

logger = ingestion_logger()

C = TypeVar("C", bound=BaseModel)


class MissingExpectedEntityAckException(Exception):
    """
    After running the ack to the sink, we got no
    Entity back
    """


class TopologyRunnerMixin(Generic[C]):
    """
    Prepares the _run function
    dynamically based on the source topology
    """

    topology: ServiceTopology
    context: TopologyContext
    metadata: OpenMetadata

    # The cache will have the shape {`child_stage.type_`: {`name`: `hash`}}
    cache = defaultdict(dict)

    def process_nodes(self, nodes: List[TopologyNode]) -> Iterable[Entity]:
        """
        Given a list of nodes, either roots or children,
        yield from its producers and process the children.

        The execution tree is created in a depth-first fashion.

        Note that this is used to handle the metadata ingestion for all our services. Therefore,
        the first Node is always expected to be a Service. This is important because:
        - Services (root Nodes) are flagged with `overwrite=False` -> In `yield_and_update_context` if
          the stage is flagged as `overwrite=False`, we won't send any PUT/PATCH request to the API if
          the service already exists.
        - Then, when we iterate over Services' children (databases, pipelines, dashboards,...), we will
          initialize a cache listing its children. This is used to to compare the fingerprint of the
          stored entity vs. incoming entity and see if we need to:
          1. Create a new entity (PUT) - if no fingerprint is found for the given name
          2. Update some fields from the entity (PATCH) - if there's a fingerprint mismatch
          3. Do nothing - if the fingerprints are the same.

        The fingerprint is stored in the db in the field `sourceHash` in each entity.

        :param nodes: Topology Nodes to process
        :return: recursively build the execution tree
        """
        for node in nodes:
            logger.debug(f"Processing node {node}")
            node_producer = getattr(self, node.producer)
            child_nodes = self._get_child_nodes(node)

            # Each node producer will give us a list of entities that we need
            # to process. Each of the internal stages will sink result to OM API.
            # E.g., in the DB topology, at the Table TopologyNode, the node_entity
            # will be each `table`
            for node_entity in node_producer() or []:
                for stage in node.stages:
                    yield from self._process_stage(
                        stage=stage, node_entity=node_entity, child_nodes=child_nodes
                    )

                # Once we are done processing all the stages,
                for stage in node.stages:
                    if stage.clear_context:
                        self.context.clear_stage(stage=stage)

                # process all children from the node being run
                yield from self.process_nodes(child_nodes)

            yield from self._run_node_post_process(node=node)

    def _get_child_nodes(self, node: TopologyNode) -> List[TopologyNode]:
        """Compute children nodes if any"""
        return (
            [get_topology_node(child, self.topology) for child in node.children]
            if node.children
            else []
        )

    def _process_stage(
        self, stage: NodeStage, node_entity: Any, child_nodes: List[TopologyNode]
    ) -> Iterable[Entity]:
        """
        For each entity produced in the Node Producer, iterate over all the Node's Stages and
        yield the assets to pass down the workflow.

        For each node_entity processed, we will cache - if needed - its children.
        E.g., when processing DB Schemas, we will store its tables to compare the fingerprint
        and decide if we need to PUT or PATCH at the sink.
        """
        logger.debug(f"Processing stage: {stage}")

        stage_fn = getattr(self, stage.processor)
        for entity_request in stage_fn(node_entity) or []:
            try:
                # yield and make sure the data is updated
                yield from self.sink_request(stage=stage, entity_request=entity_request)
            except ValueError as err:
                logger.debug(traceback.format_exc())
                logger.warning(
                    f"Unexpected value error when processing stage: [{stage}]: {err}"
                )

        if stage.cache_entities:
            self._init_cache_dict(stage=stage, child_nodes=child_nodes)

    def _run_node_post_process(self, node: TopologyNode) -> Iterable[Entity]:
        """
        If the node has post_process steps, iterate over them and yield the result
        """
        if node.post_process:
            logger.debug(f"Post processing node {node}")
            for process in node.post_process:
                try:
                    node_post_process = getattr(self, process)
                    for entity_request in node_post_process():
                        yield entity_request
                except Exception as exc:
                    logger.debug(traceback.format_exc())
                    logger.warning(
                        f"Could not run Post Process `{process}` due to [{exc}]"
                    )

    def _init_cache_dict(
        self, stage: NodeStage, child_nodes: List[TopologyNode]
    ) -> None:
        """
        Method to call the API to fill the entities cache.

        The cache will be part of the context
        """
        for child_node in child_nodes or []:
            for child_stage in child_node.stages or []:
                if child_stage.use_cache:
                    entity_fqn = self.context.fqn_from_stage(
                        stage=stage,
                        entity_name=self.context.__dict__[stage.context],
                    )

                    self.get_fqn_source_hash_dict(
                        parent_type=stage.type_,
                        child_type=child_stage.type_,
                        entity_fqn=entity_fqn,
                    )

    def get_fqn_source_hash_dict(
        self, parent_type: Type[Entity], child_type: Type[Entity], entity_fqn: str
    ) -> None:
        """
        Get all the entities and store them as fqn:sourceHash in a dict
        """
        if parent_type in (Database, DatabaseSchema):
            if child_type == StoredProcedure:
                params = {"databaseSchema": entity_fqn}
            else:
                params = {"database": entity_fqn}
        else:
            params = {"service": entity_fqn}
        entities_list = self.metadata.list_all_entities(
            entity=child_type,
            params=params,
            fields=["sourceHash"],
        )
        for entity in entities_list:
            if entity.sourceHash:
                self.cache[child_type][
                    model_str(entity.fullyQualifiedName)
                ] = entity.sourceHash

    def _iter(self) -> Iterable[Either]:
        """
        This is the implementation for the entrypoint of our Source classes, which
        are an IterStep

        Based on a ServiceTopology, find the root node
        and fetch all source methods in the required order
        to yield data to the sink
        :return: Iterable of the Entities yielded by all nodes in the topology
        """
        yield from self.process_nodes(get_topology_root(self.topology))

    def create_patch_request(
        self, original_entity: Entity, create_request: C
    ) -> PatchRequest:
        """
        Method to get the PatchRequest object
        To be overridden by the process if any custom logic is to be applied
        """
        return PatchRequest(
            original_entity=original_entity,
            new_entity=original_entity.copy(update=create_request.__dict__),
        )

    @singledispatchmethod
    def yield_and_update_context(
        self,
        right: C,
        stage: NodeStage,
        entity_request: Either[C],
    ) -> Iterable[Either[Entity]]:
        """
        Handle the process of yielding the request and validating
        that everything was properly updated.

        The default implementation is based on a get_by_name validation
        """
        entity = None
        entity_name = model_str(right.name)
        entity_fqn = self.context.fqn_from_stage(stage=stage, entity_name=entity_name)

        # If we don't want to write data in OM, we'll return what we fetch from the API.
        # This will be applicable for service entities since we do not want to overwrite the data
        same_fingerprint = False
        if not stage.overwrite and not self._is_force_overwrite_enabled():
            entity = self.metadata.get_by_name(
                entity=stage.type_,
                fqn=entity_fqn,
                fields=["*"],
            )
            if entity:
                same_fingerprint = True

        create_entity_request_hash = None

        if hasattr(entity_request.right, "sourceHash"):
            create_entity_request_hash = generate_source_hash(
                create_request=entity_request.right,
            )
            entity_request.right.sourceHash = create_entity_request_hash

        if entity is None and stage.use_cache:
            # check if we find the entity in the entities list
            entity_source_hash = self.cache[stage.type_].get(entity_fqn)
            if entity_source_hash:
                # if the source hash is present, compare it with new hash
                if entity_source_hash != create_entity_request_hash:
                    # the entity has changed, get the entity from server and make a patch request
                    entity = self.metadata.get_by_name(
                        entity=stage.type_,
                        fqn=entity_fqn,
                        fields=["*"],
                    )

                    # we return the entity for a patch update
                    if entity:
                        patch_entity = self.create_patch_request(
                            original_entity=entity, create_request=entity_request.right
                        )
                        entity_request.right = patch_entity
                else:
                    # nothing has changed on the source skip the API call
                    logger.debug(
                        f"No changes detected for {str(stage.type_.__name__)} '{entity_fqn}'"
                    )
                    same_fingerprint = True

        if not same_fingerprint:
            # We store the generated source hash and yield the request

            yield entity_request

        # We have ack the sink waiting for a response, but got nothing back
        if stage.must_return and entity is None:
            # we'll only check the get by name for entities like database service
            # without which we cannot proceed ahead in the ingestion
            tries = 3
            while not entity and tries > 0:
                entity = self.metadata.get_by_name(
                    entity=stage.type_,
                    fqn=entity_fqn,
                    fields=["*"],  # Get all the available data from the Entity
                )
                tries -= 1

            if not entity:
                # Safe access to Entity Request name
                raise MissingExpectedEntityAckException(
                    f"Missing ack back from [{stage.type_.__name__}: {entity_fqn}] - "
                    "Possible causes are changes in the server Fernet key or mismatched JSON Schemas "
                    "for the service connection."
                )

        self.context.update_context_name(stage=stage, right=right)

    @yield_and_update_context.register
    def _(
        self,
        right: AddLineageRequest,
        stage: NodeStage,
        entity_request: Either[C],
    ) -> Iterable[Either[Entity]]:
        """
        Lineage Implementation for the context information.

        There is no simple (efficient) validation to make sure that this specific
        lineage has been properly drawn. We'll skip the process for now.
        """
        yield entity_request
        self.context.update_context_name(stage=stage, right=right.edge.fromEntity)

    @yield_and_update_context.register
    def _(
        self,
        right: OMetaTagAndClassification,
        stage: NodeStage,
        entity_request: Either[C],
    ) -> Iterable[Either[Entity]]:
        """
        Tag implementation for the context information.

        We need the full OMetaTagAndClassification in the context
        to build the TagLabels during the ingestion. We need to bundle
        both CreateClassificationRequest and CreateTagRequest.
        """
        yield entity_request

        self.context.update_context_value(stage=stage, value=right)

    @yield_and_update_context.register
    def _(
        self,
        right: OMetaCustomProperties,
        stage: NodeStage,
        entity_request: Either[C],
    ) -> Iterable[Either[Entity]]:
        """Custom Property implementation for the context information"""
        yield entity_request

        self.context.update_context_value(stage=stage, value=right)

    def sink_request(
        self, stage: NodeStage, entity_request: Either[C]
    ) -> Iterable[Either[Entity]]:
        """
        Validate that the entity was properly updated or retry if
        ack_sink is flagged.

        If we get the Entity back, update the context with it.

        :param stage: Node stage being processed
        :param entity_request: Request to pass
        :return: Entity generator
        """

        # Either use the received request or the acknowledged Entity
        entity = entity_request.right if entity_request else None

        if not stage.nullable and entity is None and entity_request.left is None:
            raise ValueError("Value unexpectedly None")

        if entity_request is not None:
            # Check that we properly received a Right response to process
            if entity_request.right is not None:
                # We need to acknowledge that the Entity has been properly sent to the server
                # to update the context
                if stage.context:
                    yield from self.yield_and_update_context(
                        entity, stage=stage, entity_request=entity_request
                    )

                else:
                    yield entity_request

            else:
                # if entity_request.right is None, means that we have a Left. We yield the Either and
                # let the step take care of the
                yield entity_request

    def _is_force_overwrite_enabled(self) -> bool:
        return self.metadata.config and self.metadata.config.forceEntityOverwriting
