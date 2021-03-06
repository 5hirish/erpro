import csv
import codecs
from datetime import datetime
from celery.utils.log import get_task_logger

from foobar.service.extensions import sql_db
from foobar.worker.core import celery_task, BaseTask
from foobar.service.products.models import ErpProductsModel
from foobar.utils import get_aws_client

task_base_name = "foobar.worker."
logger = get_task_logger(__name__)


@celery_task.task(name=task_base_name + "import_products",
                  bind=False, max_retries=3, default_retry_delay=300, track_started=True,
                  base=BaseTask)
def import_products(file_name):
    """
    Celery task to stream file from bucket and iterate over to upsert data into database.
    :param file_name: S3 file name or Key
    :return: None
    """

    file_stream = None

    s3_client = get_aws_client('s3',
                               celery_task.conf.get("AWS_ACCESS_KEY"),
                               celery_task.conf.get("AWS_SECRET_ACCESS_KEY"))

    s3_bucket_name = celery_task.conf.get("AWS_S3_PRODUCT_BUCKET")

    try:
        file_object_resp = s3_client.get_object(Bucket=s3_bucket_name, Key=file_name)
        if file_object_resp is not None and file_object_resp.get("ContentLength") is not None:
            file_stream = codecs.getreader('utf-8')(file_object_resp.get("Body"))

    except Exception as e:
        # botocore.errorfactory.NoSuchKey
        logger.exception(e)

    if file_stream is not None:
        product_reader = csv.DictReader(file_stream)

        for row in product_reader:
            if "name" and "sku" in row:
                erp_product = ErpProductsModel(
                    product_sku=row.get("sku"),
                    product_name=row.get("name"),
                    product_description=row.get("description"),
                    product_modified_on=datetime.utcnow()
                )

                existing_product = ErpProductsModel.query.filter_by(product_sku=row.get("sku")).one_or_none()
                if existing_product is not None:
                    sql_db.session.merge(erp_product)
                    sql_db.session.flush()
                else:
                    sql_db.session.add(erp_product)

                logger.info("Committing data to database")

                sql_db.session.commit()

        response = s3_client.delete_object(Bucket=s3_bucket_name, Key=file_name)
        logger.info("File deleted from bucket: {0}".format(response))