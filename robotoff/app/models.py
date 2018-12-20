import datetime
import peewee
from playhouse.postgres_ext import (PostgresqlExtDatabase,
                                    BinaryJSONField)

from robotoff import settings

db = PostgresqlExtDatabase(settings.DB_NAME,
                           user=settings.DB_USER,
                           password=settings.DB_PASSWORD,
                           host=settings.DB_HOST, port=5432)


class CategorizationTask(peewee.Model):
    id = peewee.UUIDField(primary_key=True)
    product_id = peewee.CharField(max_length=100, null=False)
    predicted_category = peewee.TextField(null=False)
    confidence = peewee.FloatField(null=True)
    last_updated_at = peewee.TextField(null=False)
    attributed_at = peewee.DateTimeField(null=True)
    attributed_to_session_id = peewee.CharField(null=True, max_length=100)
    completed_at = peewee.DateTimeField(null=True)
    completed_by_session_id = peewee.CharField(null=True, max_length=100)
    annotation = peewee.IntegerField(null=True)
    outdated = peewee.BooleanField(default=False)
    category_depth = peewee.IntegerField(null=True, index=True)
    campaign = peewee.TextField(null=True, index=True)
    countries = BinaryJSONField(null=True, index=True)

    class Meta:
        database = db
        table_name = "categorization_task"

    def set_attribution(self, session_id):
        self.attributed_at = datetime.datetime.utcnow()
        self.attributed_to_session_id = session_id
        self.save()

    def set_completion(self, session_id):
        self.completed_at = datetime.datetime.utcnow()
        self.completed_by_session_id = session_id
        self.save()


MODELS = [CategorizationTask]
