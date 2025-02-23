import json
import time
from src.config.config import Config
from src.db.db import Database
from src.core.config import settings
from src.db.tables.poi import POITable
from src.utils.utils import print_info

class PublicTransportStopPreparation:
    """Class to prepare/ classify public transport stops of the GTFS dataset. It processes the stops in batches and adds the route types (e.g. 3 = bus) to classify them."""

    def __init__(self, db: Database, region: str):
        self.db = db
        self.region = region
        self.data_config = Config("public_transport_stop", region)
        self.data_config_preparation = self.data_config.preparation

    def run(self):
        """Run the public transport stop preparation."""

        # get the geometires of the study area based on the query defined in the config
        region_geoms = self.db.select(self.data_config_preparation['region'])

        print_info(f"Started to create table basic.poi_public_transport_stop_{self.region}.")
        # Create table for public transport stops
        self.db.perform(POITable(data_set_type="poi", schema_name="basic", data_set_name=f"public_transport_stop_{self.region}").create_poi_table(table_type='standard'))
        print_info(f"Created table basic.poi_public_transport_stop_{self.region}.")

        # loops through the geometries of the study area and classifies the public transport stops based on GTFS
        # loops through the gtfs stops and classifies them based on the route type in the stop_times table
        for i, geom in enumerate(region_geoms):
            ts = time.time()

            classify_gtfs_stop_sql = f"""
                INSERT INTO basic.poi_public_transport_stop_{self.region}(
                    category,
                    name,
                    source,
                    tags,
                    geom
                )
                WITH parent_station_name AS (
                    SELECT s.stop_name AS name, s.stop_id
                    FROM basic.stops s
                    WHERE ST_Intersects(s.geom, ST_SetSRID(ST_GeomFromText(ST_AsText('{geom[0]}')), 4326))
                    AND parent_station IS NULL
                ),
                clipped_gfts_stops AS (
                    SELECT p.name, s.geom, json_build_object('stop_id', s.stop_id, 'parent_station', s.parent_station, 'h3_3', s.h3_3) AS tags
                    FROM basic.stops s, parent_station_name p
                    WHERE ST_Intersects(s.geom, ST_SetSRID(ST_GeomFromText(ST_AsText('{geom[0]}')), 4326))
                    AND parent_station IS NOT NULL
                    AND s.parent_station = p.stop_id
                ),
                categorized_gtfs_stops AS (
                    SELECT route_type, c.name, c.geom, c.tags
                    FROM clipped_gfts_stops c
                    CROSS JOIN LATERAL
                    (
                        SELECT  '{json.dumps(self.data_config_preparation['classification']['gtfs_route_types'])}'::jsonb ->> r.route_type::TEXT AS route_type
                        FROM
                        (
                            SELECT DISTINCT o.route_type
                            FROM basic.stop_times_optimized o
                            WHERE o.stop_id = tags ->> 'stop_id'
                            AND o.h3_3 = (c.tags ->> 'h3_3')::integer
                            AND o.route_type IN {tuple(int(key) for key in self.data_config_preparation['classification']['gtfs_route_types'].keys())}
                        ) r
                        WHERE route_type IS NOT NULL
                        ORDER BY r.route_type
                    ) j
                )
                SELECT route_type AS category, name, NULL AS source, json_build_object('extended_source', json_build_object('stop_id', ARRAY_AGG(tags ->> 'stop_id')), 'parent_station', tags ->> 'parent_station') AS tags, ST_MULTI(ST_UNION(geom)) AS geom
                FROM categorized_gtfs_stops
                GROUP BY route_type, tags ->> 'parent_station', name
                ;
            """

            self.db.perform(classify_gtfs_stop_sql)

            te = time.time()  # End time of the iteration
            iteration_time = te - ts  # Time taken by the iteration
            print_info(f"Processing {i + 1} of {len(region_geoms)}. Iteration time: {iteration_time} seconds.")

        print_info("Stops with parent station have been classified")
        print_info("Creating temporal.remaining_stops")

        remaining_stops_sql = f"""
            DROP TABLE IF EXISTS temporal.remaining_stations;
            CREATE TABLE temporal.remaining_stations AS
            WITH processed_staions AS (
                SELECT
                    unnest(string_to_array(ppts.tags ->> 'parent_station', ',')) AS stop_id
                FROM basic.poi_public_transport_stop_{self.region} ppts
                UNION
                SELECT
                    jsonb_array_elements_text(ppts.tags -> 'extended_source' -> 'stop_id') AS stop_id
                FROM basic.poi_public_transport_stop_{self.region} ppts
            )
            SELECT s.*
            FROM basic.stops s
            LEFT JOIN processed_staions ps
            ON s.stop_id = ps.stop_id
            WHERE ps.stop_id IS NULL;
        """
        self.db.perform(remaining_stops_sql)

        print_info("temporal.remaining_stops has been created")

        # loops through the remaining stops and group them by name
        print_info("Classifying remaining stops started.")
        for i, geom in enumerate(region_geoms):
            ts = time.time()

            classify_gtfs_stop_sql = f"""
                INSERT INTO basic.poi_public_transport_stop_{self.region}(
                    category,
                    name,
                    source,
                    tags,
                    geom
                )
                WITH clipped_gfts_stops AS (
                    SELECT s.stop_name, s.geom, json_build_object('stop_id', s.stop_id, 'h3_3', s.h3_3) AS tags
                    FROM temporal.remaining_stations s
                    WHERE ST_Intersects(s.geom, ST_SetSRID(ST_GeomFromText(ST_AsText('{geom[0]}')), 4326))
                ),
                categorized_gtfs_stops AS (
                    SELECT route_type, c.stop_name, c.geom, c.tags
                    FROM clipped_gfts_stops c
                    CROSS JOIN LATERAL
                    (
                        SELECT  '{json.dumps(self.data_config_preparation['classification']['gtfs_route_types'])}'::jsonb ->> r.route_type::TEXT AS route_type
                        FROM
                        (
                            SELECT DISTINCT o.route_type
                            FROM basic.stop_times_optimized o
                            WHERE o.stop_id = tags ->> 'stop_id'
                            AND o.h3_3 = (c.tags ->> 'h3_3')::integer
                            AND o.route_type IN {tuple(int(key) for key in self.data_config_preparation['classification']['gtfs_route_types'].keys())}
                        ) r
                        WHERE route_type IS NOT NULL
                        ORDER BY r.route_type
                    ) j
                )
                SELECT route_type AS category, stop_name AS name, NULL AS source, json_build_object('extended_source', json_build_object('stop_id', ARRAY_AGG(tags ->> 'stop_id'))) AS tags, ST_MULTI(ST_UNION(geom)) AS geom
                FROM categorized_gtfs_stops
                GROUP BY route_type, stop_name
                ;
            """

            self.db.perform(classify_gtfs_stop_sql)

            te = time.time()  # End time of the iteration
            iteration_time = te - ts  # Time taken by the iteration
            print_info(f"Processing {i + 1} of {len(region_geoms)}. Iteration time: {iteration_time} seconds.")

def prepare_public_transport_stop(region: str):

    db_rd = Database(settings.RAW_DATABASE_URI)
    public_transport_stop_preparation = PublicTransportStopPreparation(db=db_rd, region=region)
    public_transport_stop_preparation.run()
    db_rd.conn.close()

