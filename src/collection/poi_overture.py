import time

from pyspark.sql.functions import col, expr, to_json
from pyspark.sql.types import TimestampType
from sedona.spark import SedonaContext

from src.collection.overture_collection_base import OvertureBaseCollection
from src.core.config import settings
from src.db.db import Database
from src.utils.utils import get_region_bbox_coords, print_error, print_info, timing


class OverturePOICollection(OvertureBaseCollection):

    def __init__(self, db_local, db_remote, region, collection_type):
        super().__init__(db_local, db_remote, region, collection_type)

    def initialize_data_source(self, sedona: SedonaContext):
        """Initialize Overture geoparquet file source and data frames for places data."""

        # Load Overture geoparquet data into Spark DataFrames
        self.places_df = sedona.read.format("geoparquet").load(
            path=f"{self.data_config_collection['source']}/type=*/*"
        )

        print_info("Initialized data source.")

    def initialize_tables(self):
        """Create table in PostgreSQL database for places."""

        sql_create_table_places = f"""
            DROP TABLE IF EXISTS temporal.places_{self.region}_raw_no_geom;
            CREATE TABLE temporal.places_{self.region}_raw_no_geom (
                id TEXT PRIMARY KEY,
                categories TEXT,
                updatetime TIMESTAMPTZ,
                version INT,
                names TEXT,
                confidence DOUBLE PRECISION,
                websites TEXT,
                socials TEXT,
                emails TEXT,
                phones TEXT,
                brand TEXT,
                addresses TEXT,
                sources TEXT,
                geometry TEXT
            );
        """
        self.db_local.perform(sql_create_table_places)
        print_info(f"Created table: temporal.places_{self.region}_raw_no_geom.")

    def filter_region_places(self, bbox_coords: dict):
        """Initialize the places dataframe and apply relevant filters."""

        # Select the necessary columns
        places = self.places_df.selectExpr(
            "id",
            "updatetime",
            "version",
            "names",
            "categories",
            "confidence",
            "websites",
            "socials",
            "emails",
            "phones",
            "brand",
            "addresses",
            "sources",
            "ST_AsText(geometry) AS geometry",
            "bbox"
        )

        places = self.places_df.filter(
            (places.bbox.minx > bbox_coords["xmin"]) &
            (places.bbox.miny > bbox_coords["ymin"]) &
            (places.bbox.maxx < bbox_coords["xmax"]) &
            (places.bbox.maxy < bbox_coords["ymax"])
        )
        places = places.drop(places.bbox)

        # Convert the complex types to JSON strings
        complex_columns = [
            "updatetime",
            "names",
            "categories",
            "brand",
            "addresses",
            "sources",
            "websites",
            "socials",
            "emails",
            "phones",
        ]

        for column in complex_columns:
            if column == "updatetime":
                places = places.withColumn(column, col(column).cast(TimestampType()))
            else:
                places = places.withColumn(column, to_json(column))

        places = places.withColumn("geometry", expr("ST_AsText(geometry)"))

        return places

    @timing
    def alter_tables(self):
        """Alter table in PostgreSQL database for places."""

        print_info(f"Starting to alter tables temporal.places_{self.region}_raw and temporal.places_{self.region}.")

        # get the geometires of the study area based on the query defined in the config
        region_geoms = self.db_local.select(self.data_config_collection['region'])

        # create index on places raw
        create_table_with_geom_sql = f"""
            DROP TABLE IF EXISTS temporal.places_{self.region}_raw;
            CREATE UNLOGGED TABLE temporal.places_{self.region}_raw AS
            SELECT id, categories, updatetime, version, names, confidence, websites, socials, emails, phones, brand, addresses, sources, ST_SetSRID(ST_GeomFromText(geometry), 4326) AS geometry
            FROM temporal.places_{self.region}_raw_no_geom;
            CREATE INDEX ON temporal.places_{self.region}_raw USING GIST (geometry);
        """
        self.db_local.perform(create_table_with_geom_sql)
        print_info(f"Created new unlogged table temporal.places_{self.region}_raw with converted geometry")

        # create table for the Overture places
        create_place_table_sql = f"""
            DROP TABLE IF EXISTS temporal.places_{self.region};
            CREATE TABLE temporal.places_{self.region} AS (
                SELECT *
                FROM temporal.places_{self.region}_raw
                WHERE 1=0
            );
            ALTER TABLE temporal.places_{self.region}
            ADD COLUMN IF NOT EXISTS other_categories varchar[],
            ADD COLUMN IF NOT EXISTS street varchar,
            ADD COLUMN IF NOT EXISTS housenumber varchar,
            ADD COLUMN IF NOT EXISTS zipcode varchar;
        """
        self.db_local.perform(create_place_table_sql)

        print_info(f"created table temporal.places_{self.region} including geometry index and pkey")

        cur = self.db_local.conn.cursor()

        for index, geom in enumerate(region_geoms, start=1):
            start_time = time.time()

            clip_poi_overture = f"""
                INSERT INTO temporal.places_{self.region} (id, names, other_categories, categories, street, housenumber, zipcode, brand, updatetime, version, confidence, websites, socials, emails, phones, addresses, sources, geometry)
                WITH region AS (
                    SELECT ST_SetSRID(ST_GeomFromText(ST_AsText('{geom[0]}')), 4326) AS geom
                ),
                new_pois AS (
                    SELECT DISTINCT ON (p.id) p.*
                    FROM temporal.places_{self.region}_raw p
                    JOIN region r ON ST_Intersects(p.geometry, r.geom)
                )
                SELECT
                    np.id,
                    TRIM(BOTH '"' FROM (np.names::jsonb->'common'->0->'value')::text) AS names,
                    CASE
                        WHEN (np.categories::jsonb->'alternate'->>0) IS NOT NULL OR (np.categories::jsonb->'alternate'->>1) IS NOT NULL THEN
                            ARRAY_REMOVE(ARRAY_REMOVE(ARRAY[(np.categories::jsonb->'alternate'->>0)::varchar, (np.categories::jsonb->'alternate'->>1)::varchar], NULL), '')
                        ELSE
                            ARRAY[]::varchar[]
                    END AS other_categories,
                    TRIM(BOTH '"' FROM (np.categories::jsonb->>'main')) AS categories,
                    TRIM(substring((np.addresses::jsonb->0->>'freeform')::varchar from '^(.*)(?=\s\d)')) AS street,
                    TRIM(substring((np.addresses::jsonb->0->>'freeform')::varchar from '(\s\d.*)$')) AS housenumber,
                    (np.addresses::jsonb->0->>'postcode')::varchar AS zipcode,
                    np.brand::jsonb->'names'->'common'->0->>'value' AS brand,
                    np.updatetime,
                    np.version,
                    np.confidence,
                    np.websites,
                    np.socials,
                    np.emails,
                    np.phones,
                    np.addresses,
                    np.sources,
                    np.geometry
                FROM new_pois np
            """

            try:
                cur.execute(clip_poi_overture)
                self.db_local.conn.commit()
            except Exception as e:
                print(f"An error occurred: {e}")
                self.db_local.conn.rollback()

            end_time = time.time()
            elapsed_time = end_time - start_time
            print_info(f"Processing geom {index} out of {len(region_geoms)}. This iteration took {elapsed_time} seconds.")

        cur.close()

        # Convert unlogged table to regular table
        convert_to_regular_table_sql = f"""
            ALTER TABLE temporal.places_{self.region}_raw SET LOGGED;
            ALTER TABLE temporal.places_{self.region} ADD PRIMARY KEY (id);
            CREATE INDEX ON temporal.places_{self.region} USING GIST (geometry);
        """
        self.db_local.perform(convert_to_regular_table_sql)
        print_info(f"Converted temporal.places_{self.region}_raw to a regular table")


    def run(self):
        """Run Overture places collection."""

        sedona = self.initialize_sedona_context()
        self.initialize_jdbc_properties()
        self.initialize_data_source(sedona)
        self.initialize_tables()

        bbox_coords = get_region_bbox_coords(
            geom_query=f"""SELECT ST_Union(geom) AS geom FROM ({self.data_config_collection["region"]}) AS subquery""",
            db=self.db_local
        )
        region_places = self.filter_region_places(bbox_coords)

        self.fetch_data(
            data_frame=region_places,
            output_schema="temporal",
            output_table=f"places_{self.region}_raw_no_geom"
        )

        self.alter_tables()

        print_info(f"Finished Overture places collection for: {self.region}.")

def collect_poi_overture(region: str):
    print_info(f"Collect Overture places data for region: {region}.")
    db_local = Database(settings.LOCAL_DATABASE_URI)
    db_remote = Database(settings.RAW_DATABASE_URI)

    try:
        OverturePOICollection(
            db_local=db_local,
            db_remote=db_remote,
            region=region,
            collection_type="poi_overture"
        ).run()
        db_local.close()
        db_remote.close()
        print_info("Finished Overture places collection.")
    except Exception as e:
        print_error(e)
        raise e
    finally:
        db_local.close()
        db_remote.close()
