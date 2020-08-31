import itertools
import logging
import time
from datetime import datetime
from collections import defaultdict
from snuba.datasets.storages import StorageKey
from typing import (
    Any,
    Callable,
    Mapping,
    MutableSequence,
    NamedTuple,
    Optional,
    Sequence,
    Union,
    cast,
)

import rapidjson
from confluent_kafka import Producer as ConfluentKafkaProducer

from snuba.clickhouse.http import JSONRow, JSONRowEncoder
from snuba.datasets.message_filters import StreamMessageFilter
from snuba.datasets.storage import WritableTableStorage
from snuba.processor import (
    InsertBatch,
    MessageProcessor,
    ProcessedMessage,
    ReplacementBatch,
)
from snuba.utils.metrics.backends.abstract import MetricsBackend
from snuba.utils.streams.batching import AbstractBatchWorker
from snuba.utils.streams.kafka import KafkaPayload
from snuba.utils.streams.processing import ProcessingStrategy, ProcessingStrategyFactory
from snuba.utils.streams.streaming import (
    CollectStep,
    FilterStep,
    ProcessingStep,
    TransformStep,
)
from snuba.utils.streams.types import Message, Partition, Topic
from snuba.writer import BatchWriter, BatchWriterEncoderWrapper, WriterTableRow

try:
    # PickleBuffer is only available in Python 3.8 and above and when using the
    # pickle protocol version 5 and greater.
    from pickle import PickleBuffer
except ImportError:
    pass


logger = logging.getLogger("snuba.consumer")


class KafkaMessageMetadata(NamedTuple):
    offset: int
    partition: int
    timestamp: datetime


class InvalidActionType(Exception):
    pass


class ConsumerWorker(AbstractBatchWorker[KafkaPayload, ProcessedMessage]):
    def __init__(
        self,
        storage: WritableTableStorage,
        metrics: MetricsBackend,
        producer: Optional[ConfluentKafkaProducer] = None,
        replacements_topic: Optional[Topic] = None,
    ) -> None:
        self.__storage = storage
        self.producer = producer
        self.replacements_topic = replacements_topic
        self.metrics = metrics
        table_writer = storage.get_table_writer()
        self.__writer = BatchWriterEncoderWrapper(
            table_writer.get_writer(
                metrics, {"load_balancing": "in_order", "insert_distributed_sync": 1}
            ),
            JSONRowEncoder(),
        )

        self.__pre_filter = table_writer.get_stream_loader().get_pre_filter()
        self.__processor = (
            self.__storage.get_table_writer().get_stream_loader().get_processor()
        )

    def process_message(
        self, message: Message[KafkaPayload]
    ) -> Optional[ProcessedMessage]:

        if self.__pre_filter and self.__pre_filter.should_drop(message):
            return None

        return self._process_message_impl(
            rapidjson.loads(message.payload.value),
            KafkaMessageMetadata(
                offset=message.offset,
                partition=message.partition.index,
                timestamp=message.timestamp,
            ),
        )

    def _process_message_impl(
        self, value: Mapping[str, Any], metadata: KafkaMessageMetadata,
    ) -> Optional[ProcessedMessage]:
        return self.__processor.process_message(value, metadata)

    def delivery_callback(self, error, message):
        if error is not None:
            # errors are KafkaError objects and inherit from BaseException
            raise error

    def flush_batch(self, batch: Sequence[ProcessedMessage]):
        """First write out all new INSERTs as a single batch, then reproduce any
        event replacements such as deletions, merges and unmerges."""
        inserts: MutableSequence[WriterTableRow] = []
        replacements: MutableSequence[ReplacementBatch] = []

        for item in batch:
            if isinstance(item, InsertBatch):
                inserts.extend(item.rows)
            elif isinstance(item, ReplacementBatch):
                replacements.append(item)
            else:
                raise TypeError(f"unexpected type: {type(item)!r}")

        if inserts:
            self.__writer.write(inserts)

            self.metrics.timing("inserts", len(inserts))

        if replacements:
            for replacement in replacements:
                key = replacement.key.encode("utf-8")
                for value in replacement.values:
                    self.producer.produce(
                        self.replacements_topic.name,
                        key=key,
                        value=rapidjson.dumps(value).encode("utf-8"),
                        on_delivery=self.delivery_callback,
                    )

            self.producer.flush()


class JSONRowInsertBatch(NamedTuple):
    rows: Sequence[JSONRow]

    def __reduce_ex__(self, protocol: int):
        if protocol >= 5:
            return (type(self), ([PickleBuffer(row) for row in self.rows],))
        else:
            return type(self), (self.rows,)


class InsertBatchWriter(ProcessingStep[JSONRowInsertBatch]):
    def __init__(self, writer: BatchWriter[JSONRow]) -> None:
        self.__writer = writer

        self.__messages: MutableSequence[Message[JSONRowInsertBatch]] = []
        self.__closed = False

    def poll(self) -> None:
        pass

    def submit(self, message: Message[JSONRowInsertBatch]) -> None:
        assert not self.__closed

        self.__messages.append(message)

    def close(self) -> None:
        self.__closed = True

        if not self.__messages:
            return

        self.__writer.write(
            itertools.chain.from_iterable(
                message.payload.rows for message in self.__messages
            )
        )

    def join(self, timeout: Optional[float] = None) -> None:
        pass


class ReplacementBatchWriter(ProcessingStep[ReplacementBatch]):
    def __init__(self, producer: ConfluentKafkaProducer, topic: Topic) -> None:
        self.__producer = producer
        self.__topic = topic

        self.__messages: MutableSequence[Message[ReplacementBatch]] = []
        self.__closed = False

    def poll(self) -> None:
        pass

    def submit(self, message: Message[ReplacementBatch]) -> None:
        assert not self.__closed

        self.__messages.append(message)

    def __delivery_callback(self, error, message) -> None:
        if error is not None:
            # errors are KafkaError objects and inherit from BaseException
            raise error

    def close(self) -> None:
        self.__closed = True

        if not self.__messages:
            return

        for message in self.__messages:
            batch = message.payload
            key = batch.key.encode("utf-8")
            for value in batch.values:
                self.__producer.produce(
                    self.__topic.name,
                    key=key,
                    value=rapidjson.dumps(value).encode("utf-8"),
                    on_delivery=self.__delivery_callback,
                )

    def join(self, timeout: Optional[float] = None) -> None:
        args = []
        if timeout is not None:
            args.append(timeout)

        self.__producer.flush(*args)


class ProcessedMessageBatchWriter(
    ProcessingStep[Union[None, JSONRowInsertBatch, ReplacementBatch]]
):
    def __init__(
        self,
        insert_batch_writers: Mapping[StorageKey, InsertBatchWriter],
        replacement_batch_writer: Optional[ReplacementBatchWriter] = None,
    ) -> None:
        self.__insert_batch_writers = insert_batch_writers

        self.__replacement_batch_writer = replacement_batch_writer

        self.__closed = False

    def poll(self) -> None:
        for key in self.__insert_batch_writers:
            self.__insert_batch_writers[key].poll()

        if self.__replacement_batch_writer is not None:
            self.__replacement_batch_writer.poll()

    def submit(
        self, message: Message[Union[None, JSONRowInsertBatch, ReplacementBatch]]
    ) -> None:
        assert not self.__closed

        if message.payload is None:
            return

        rows = defaultdict(list)
        for key, row in message.payload.rows:
            rows[key].append(row)
        for key, row_list in rows.items():
            self.__insert_batch_writers[key].submit(
                cast(
                    Message[JSONRowInsertBatch],
                    Message(
                        message.partition,
                        message.offset,
                        JSONRowInsertBatch(row_list),
                        message.timestamp,
                    ),
                )
            )
        # if isinstance(message.payload, JSONRowInsertBatch):
        #    self.__insert_batch_writer.submit(
        #        cast(Message[JSONRowInsertBatch], message)
        #    )
        if isinstance(message.payload, ReplacementBatch):
            if self.__replacement_batch_writer is None:
                raise TypeError("writer not configured to support replacements")

            self.__replacement_batch_writer.submit(
                cast(Message[ReplacementBatch], message)
            )
        # else:
        #    raise TypeError("unexpected payload type")

    def close(self) -> None:
        self.__closed = True

        for key in self.__insert_batch_writers:
            self.__insert_batch_writers[key].close()

        if self.__replacement_batch_writer is not None:
            self.__replacement_batch_writer.close()

    def join(self, timeout: Optional[float] = None) -> None:
        start = time.time()
        for key in self.__insert_batch_writers:
            self.__insert_batch_writers[key].join(timeout)

        if self.__replacement_batch_writer is not None:
            if timeout is not None:
                timeout = max(timeout - (time.time() - start), 0)

            self.__replacement_batch_writer.join(timeout)


json_row_encoder = JSONRowEncoder()


class StreamingConsumerStrategyFactory(ProcessingStrategyFactory[KafkaPayload]):
    def __init__(
        self,
        prefilter: Optional[StreamMessageFilter[KafkaPayload]],
        processor: MessageProcessor,
        writers: Union[Mapping[StorageKey, BatchWriter[JSONRow]], BatchWriter[JSONRow]],
        max_batch_size: int,
        max_batch_time: float,
        replacements_producer: Optional[ConfluentKafkaProducer] = None,
        replacements_topic: Optional[Topic] = None,
    ) -> None:
        self.__prefilter = prefilter
        self.__processor = processor
        self.__writers = writers

        self.__max_batch_size = max_batch_size
        self.__max_batch_time = max_batch_time

        assert not (replacements_producer is None) ^ (replacements_topic is None)
        self.__supports_replacements = replacements_producer is not None
        self.__replacements_producer = replacements_producer
        self.__replacements_topic = replacements_topic

    def __should_accept(self, message: Message[KafkaPayload]) -> bool:
        assert self.__prefilter is not None
        return not self.__prefilter.should_drop(message)

    def __process_message(
        self, message: Message[KafkaPayload]
    ) -> Union[None, JSONRowInsertBatch, ReplacementBatch]:
        result = self.__processor.process_message(
            rapidjson.loads(message.payload.value),
            KafkaMessageMetadata(
                message.offset, message.partition.index, message.timestamp
            ),
        )

        if isinstance(result, InsertBatch):
            return JSONRowInsertBatch(
                [
                    (key, json_row_encoder.encode(payload))
                    for key, payload in result.rows
                ]
            )
        else:
            return result

    def __build_write_step(self) -> ProcessedMessageBatchWriter:
        batch_writers = {
            key: InsertBatchWriter(writer) for key, writer in self.__writers.items()
        }

        replacement_batch_writer: Optional[ReplacementBatchWriter]
        if self.__supports_replacements:
            assert self.__replacements_producer is not None
            assert self.__replacements_topic is not None
            replacement_batch_writer = ReplacementBatchWriter(
                self.__replacements_producer, self.__replacements_topic
            )
        else:
            replacement_batch_writer = None

        return ProcessedMessageBatchWriter(batch_writers, replacement_batch_writer)

    def create(
        self, commit: Callable[[Mapping[Partition, int]], None]
    ) -> ProcessingStrategy[KafkaPayload]:
        strategy: ProcessingStrategy[KafkaPayload] = TransformStep(
            self.__process_message,
            CollectStep(
                self.__build_write_step,
                commit,
                self.__max_batch_size,
                self.__max_batch_time,
            ),
        )

        if self.__prefilter is not None:
            strategy = FilterStep(self.__should_accept, strategy)

        return strategy
