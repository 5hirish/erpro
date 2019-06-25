import uuid

from flask import Blueprint, request, jsonify, current_app
from flask_restplus import Api, Resource, fields
from sqlalchemy.sql.expression import or_
from botocore.exceptions import NoCredentialsError, ClientError

from foobar.service.extensions import sql_db
from foobar.service.schemas import success_schema, error_schema
from foobar.service.products.schemas import product_list_response_schema, product_search_payload_schema
from foobar.service.products.models import ErpProductsModel
from foobar.worker.tasks import import_products
from foobar.utils import get_aws_client

blue_print_name = 'products'
blue_print_prefix = '/products'
ns_prefix_v1 = 'v1'

Model = sql_db.Model

# ---------- BluePrints -----------
product_blueprint = Blueprint(blue_print_name, __name__, url_prefix=blue_print_prefix)

# ---------- APIs -----------
api_v1 = Api(product_blueprint, version='1.0', title='Product APIs', doc='/doc/',
             description='APIs to create/read/update/delete products',
             )

# ---------- Namespaces -----------
api_v1_ns = api_v1.namespace(ns_prefix_v1, description='Product CRUD operations')

# ---------- Models -----------
success_model = api_v1_ns.model('Success', success_schema)
error_model = api_v1_ns.model('Error', error_schema)

product_import_model = api_v1_ns.model('Product upload and import from file',
                                       product_search_payload_schema)

fb_connect_account_success_model = api_v1_ns.inherit('Fetch filtered products', success_model,
                                                     product_list_response_schema)


@product_blueprint.before_request
def perform_before_request():
    # Operations to be performed before each request
    pass


@api_v1_ns.route('/<string:product_sku>')
class ProductsCollection(Resource):
    """
    Products resource collection, performing CRUD on the collection or resource.
    """

    product_status_types = ["active", "inactive"]
    allowed_file_extensions = ['csv']

    @api_v1_ns.response(code=202, model=success_model, description="Import Job scheduled")
    @api_v1_ns.response(code=424, model=error_model, description="Error schema")
    @api_v1_ns.response(code=413, model=error_model, description="Error schema")
    @api_v1_ns.response(code=400, model=error_model, description="Error schema")
    def get(self, product_sku=None):
        """
        View all of the products.
        Search products by name or by sku or by both
        Filter products by status
        Default page 1 and limit 100
        :param product_sku: Product SKU
        :return: Pagination
        """
        page_limit, page_num = 100, 1

        product_sku = request.args.get("sku")
        product_name = request.args.get("name", "null")
        product_status = request.args.get("status")

        page_limit_str = request.args.get("limit", 100)
        page_num_str = request.args.get("page", 1)

        if page_limit_str is not None and page_limit_str != "":
            try:
                page_limit = min(int(page_limit_str), 100)
            except ValueError:
                page_limit = 100

        if page_num_str is not None and page_num_str != "":
            try:
                page_num = int(page_num_str)
            except ValueError:
                page_num = 100

        if 0 < page_limit <= 1000 and 0 < page_num:

            if product_sku is None and product_name == "null":

                products_cursor = ErpProductsModel.query

            else:
                products_cursor = ErpProductsModel.query.filter(
                    or_(
                        ErpProductsModel.productName.ilike('%' + product_name + '%'),
                        ErpProductsModel.productSKU == product_sku
                    ))

            if product_status is not None and product_status != "":
                products_cursor = products_cursor.filter_by(productStatus=product_status)

            total_products = products_cursor.count()
            page_products = products_cursor.paginate(page_num, per_page=page_limit, error_out=False)

            if page_products is not None and page_products.items is not None and len(page_products.items) > 0:
                list_products = []
                for cur_product in page_products.items:
                    product = {
                        "sku": cur_product.productSKU,
                        "name": cur_product.productName,
                        "description": cur_product.productDescription,
                        "status": cur_product.productStatus,
                        "modifiedOn": cur_product.productModifiedOn
                    }

                    list_products.append(product)

                return jsonify({
                    "status": "success",
                    "msg": "Fetched products {0}".format(len(list_products)),
                    "totalProducts": total_products,
                    "data": list_products
                }), 200

            return jsonify({
                "status": "failure",
                "msg": "No products found matching the criteria",
                "errorCode": "NOT_FOUND"
            }), 404

        return jsonify(
            {
                "status": "failure",
                "msg": "Invalid page parameters",
                "errorCode": "INVALID_PAYLOAD"
            }
        ), 400

    def patch(self, product_sku=None):
        """
        Mark products active/inactvie products
        :param product_sku: Product SKU
        :return:
        """

        product_status = request.args.get("status", "").lower()

        if product_sku is not None and product_sku != "" and product_status is not None \
                and product_status in self.product_status_types:

            product = sql_db.session.query(ErpProductsModel.productSKU == product_sku).one_or_none()
            if product is not None:
                product.productStatus = product_status
                sql_db.session.commit()

                return jsonify(
                    {
                        "status": "success",
                        "msg": "Updated products status",
                    }
                ), 200

        return jsonify(
            {
                "status": "failure",
                "msg": "Invalid page parameters",
                "errorCode": "INVALID_PAYLOAD"
            }
        ), 400

    def put(self, product_sku=None):
        """
        Import CSV file of products and upsert the data.
        SKU is case insensitive.
        :param product_sku: Product SKU
        :return: Job scheduled acknowledgement
        """

        file_type = 'csv'.lower()

        if file_type is not None and not request.stream.is_exhausted \
                and file_type in self.allowed_file_extensions:

            s3_client = get_aws_client('s3',
                                       current_app.config.get("AWS_ACCESS_KEY"),
                                       current_app.config.get("AWS_SECRET_ACCESS_KEY"))

            s3_bucket_name = current_app.config.get("AWS_S3_PRODUCT_BUCKET")

            current_app.logger.debug("Stream Size:{0}".format(request.stream.limit))

            is_bucket_present = False
            try:
                bucket_head = s3_client.head_bucket(Bucket=s3_bucket_name)
                if bucket_head.get("ResponseMetadata") is not None:
                    is_bucket_present = bucket_head.get("ResponseMetadata").get("HTTPStatusCode") == 200
            except NoCredentialsError as err:
                current_app.logger.exception(err)
            except ClientError as err:
                current_app.logger.exception(err)

            if request.stream.limit <= current_app.config.get("FILE_STREAM_LIMIT") and is_bucket_present:

                unique_file_name = str(uuid.uuid4())
                file_name = unique_file_name + "." + file_type

                s3_client.upload_fileobj(request.stream, s3_bucket_name, file_name)

                if current_app.config.get("CELERY_TASK_ALWAYS_EAGER"):
                    import_products.s(file_name).apply()
                    current_app.logger.info("Task executed synchronously")
                else:
                    current_app.logger.info("Task sent to celery worker")
                    import_products.delay(file_name)

                return jsonify(
                    {
                        "status": "success",
                        "msg": "Products scheduled for import",
                    }
                ), 202

            elif not is_bucket_present:
                return jsonify({
                    "status": "failure",
                    "errorCode": "BUCKET_FAILED",
                    "msg": "Failed to connect to Storage bucket",
                }), 424
            else:
                return jsonify(
                    {
                        "status": "failure",
                        "errorCode": "STREAM_LIMIT_EXCEEDED",
                        "msg": "Products upload is limited to 50GB",
                    }
                ), 413

        return jsonify(
            {
                "status": "failure",
                "errorCode": "INVALID_PAYLOAD",
                "msg": "Products importing failed",
            }
        ), 400

    def delete(self, product_sku=None):
        """
            Delete all existing products
            :return:
            """
        if product_sku is not None:
            product = sql_db.session.query(ErpProductsModel.productSKU == product_sku).one_or_none()
            product.delete()

        else:
            product_all = sql_db.session.query(ErpProductsModel).all()
            product_all.delete()

        sql_db.session.commit()

        return jsonify(
            {
                "status": "success",
                "msg": "Deleted all products".format(),
            }
        ), 200