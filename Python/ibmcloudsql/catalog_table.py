import time
import logging
logger = logging.getLogger(__name__)

class HiveMetastore():
    def __init__(self, target_url):
        self.current_table_name = None
        # keep tracks of what tables are availables
        self.partitioned_tables = set()
        self.regular_tables = set()
        self.sql_stmt_show_temmplate = """
        SHOW TABLES {like}
        INTO {cos_out} STORED AS CSV
        """
        # The default URL where data should be queried
        self.cos_in_url_partitioned = "cos://us-geo/sql/customers_partitioned.csv"
        self.cos_in_url = "cos://us-geo/sql/customers.csv"

        self.sql_stmt_create_template = """
        CREATE TABLE {table_name}
        USING CSV
        LOCATION {cos_in}
        """
        self.sql_stmt_show_temmplate = """
        SHOW TABLES
        INTO {cos_out} STORED AS CSV
        """
        self._target_url = target_url

    def configure(self, target_url):
        self._target_url = target_url

    @property
    def target_url(self):
        return self._target_url
    @target_url.setter
    def target_url(self, target_url):
        self._target_url = target_url

    # Extended functionality
    def show_tables(self, tartget_cos_url=None, pattern=None):
        """List the available Hive Metastore

        Parameters
        ------------
        target_cos_url: string, optional
            The COR URL where the information about the tables are stored
        pattern: str, optional
            If provided, this should be a pattern being used in name matching, e.g. '*cus*', which finds all tables with the name has 'cus'

        """
        if tartget_cos_url is None:
            cos_out = self.target_url
        sql_stmt_show = self.sql_stmt_show_temmplate.format(
            like="LIKE '{}'".format(pattern) if pattern else "",
            cos_out=cos_out)
        df = None
        try:
            job_id = self.submit_sql(sql_stmt_show)
            sql_status = self.wait_for_job(job_id)
            if sql_status == "failed":
                logger.debug(sql_stmt_show)
            elif sql_status == "completed":
                df = self.get_result(job_id)
            else:
                pass
        except Exception:
            logger.info("Fail at SHOW TABLE")
        return df

    def drop_all_tables(self):
        """
        Delete all created tables in the project

        Returns
        -------
        bool
            True [if success]
        """
        df = self.show_tables()
        if df is not None and not df.empty:
            for (_, table_name) in df["tableName"].iteritems():
                self.drop_table(table_name)
                try:
                    self.partitioned_tables.remove(table_name)
                    self.regular_tables.remove(table_name)
                except KeyError:
                    pass
            self.partitioned_tables = set()
            self.regular_tables = set()
        return True

    def drop_tables(self, table_names):
        """
        Drop a list of tables

        Parameters
        ----------
            table_names: list
                A list of tables
        """
        for x in table_names:
            self.drop_table(x)

    def drop_table(self, table_name=None):
        """
        Drop a given table

        Parameters
        ----------
        table_name: str, optional
            The name of the table
            If skipped, the being tracked catalog-table is used

        Returns
        -------
            none
        """
        if table_name is None and self.current_table_name is None:
            print("ERROR: please provide table_name")
        if table_name is None:
            table_name = self.current_table_name
            self.current_table_name = None
        sql_stmt_drop = """
        DROP TABLE {table_name}""".format(table_name=table_name)
        try:
            _, job_id = self.execute_sql(sql_stmt_drop)
            logger.debug("Job_id ({stmt}): {job_id}".format(stmt=sql_stmt_drop,
                                                            job_id=job_id))
        except Exception:
            # There is a bug - the DROP TABLE statement does not need a returned COS URI - but it seems the Python API looks for that
            pass
        try:
            self.partitioned_tables.remove(table_name)
            self.regular_tables.remove(table_name)
        except KeyError:
            pass

    def create_table(self, table_name, cos_url=None, force_recreate=False, blocking=True):
        """Create a table for data on COS

        Parameters
        ----------
        table_name: str
            The name of the table

        cos_url : str, optional
            The COS URL from which the table should reference to
            If not provided, it uses the internal `self.cos_in_url`

        force_recreate: bool, optional
            (True) force to recreate an existing table
        blocking: bool, optional
            (True) wait until it returns the resut

        Returns
        -------
            none if job "failed"
            otherwise returns
        """
        def create_table_async(
                                    table_name,
                                    cos_url=None,
                                    force_recreate=False):
            """
            the async version of `create_table`

            Parameters
            ----------
            table_name: str
                The name of the table

            cos_url : str, optional
                The COS URL from which the table should reference to
                If not provided, it uses the internal `self.cos_in_url`

            force_recreate: bool, optional
                (True) force to recreate an existing table

            Returns
            ------
                job_id [if the table is being created]
                None   [if the table already created]
            """
            self.current_table_name = table_name

            if cos_url is None:
                cos_url = self.cos_in_url

            # from IPython.display import display
            df = self.show_tables()
            try:
                found = df[df['tableName'].str.contains(table_name)]
            except Exception:
                # not found
                found = []

            # if logger.getEffectiveLevel() == logging.DEBUG:
            #     display(df)
            if len(found) > 0 and force_recreate:
                self.drop_table(table_name)
            self.regular_tables.add(table_name)
            if len(found) == 0 or force_recreate:
                sql_stmt_create = self.sql_stmt_create_template.format(
                    table_name=table_name, cos_in=cos_url)
                logger.debug(sql_stmt_create)
                job_id = self.submit_sql(sql_stmt_create)
                return job_id
            return None

        job_id = create_table_async(table_name,
                                              cos_url,
                                              force_recreate=force_recreate)
        if job_id is not None and blocking is True:
            job_status = self.wait_for_job(job_id)
            if job_status == "completed":
                return self.get_result(job_id)
            else:
                return None
        return None

    def create_partitioned_table(self, table_name, cos_url=None, force_recreate=False):
        """
        Create a partitioned table for data on COS. The data needs to be organized in the form that
        match HIVE metastore criteria, e.g.

        .. code-block:: console

            <COS-URL>/field_1=value1_1/field_2=value_2_1/object_file
            <COS-URL>/field_1=value1_2/field_2=value_2_1/object_file

        NOTE: Each time the data is updated, we need to call `recover_table_partitions` on the created partitioned table.

        Parameters
        --------------
        table_name: str
            the name of the table to be created

        cos_url : str, optional
            The COS URL from which the table should reference to
            If not provided, it uses the internal `self.cos_in_url`

        force_recreate: bool
            (True) force to recreate an existing table

        Returns
        ----------

        """
        self.current_table_name = table_name

        if cos_url is None:
            cos_url = self.cos_in_url_partitioned

        df = self.show_tables()
        try:
            found = df[df['tableName'].str.contains(table_name)]
        except Exception:
            # not found
            found = []
        if len(found) > 0 and force_recreate:
            self.drop_table(table_name)
        self.partitioned_tables.add(table_name)
        if len(found) == 0 or force_recreate:
            if True:
                # auto-detection of scheme
                self.sql_stmt_create_partitioned_template = """
                CREATE TABLE {table_name}
                USING CSV
                LOCATION {cos_in}
                """
                sql_stmt_create_partitioned = self.sql_stmt_create_partitioned_template.format(
                    table_name=table_name, cos_in=cos_url)
            else:
                # explit selection of scheme -> need to tell "PARTITIONED BY"
                sql_stmt_create_partitioned = """
                CREATE TABLE {table_name} (
                customerID string,
                companyName string,
                contactName string,
                contactTitle string,
                address string,
                region string,
                postalCode string,
                country string,
                phone string,
                fax string
                )
                USING CSV
                PARTITIONED BY (country)
                LOCATION {cos_in}
                """.format(table_name=table_name, cos_in=cos_url)
            logger.debug(sql_stmt_create_partitioned)
            self.run_sql(sql_stmt_create_partitioned)
            time.sleep(2)
            self.recover_table_partitions(table_name)

    def recover_table_partitions(self, table_name):
        """
        This step is required after creating a new partitioned table

        Parameters
        ----------
            table_name: str
                The partitioned table name
        """
        self.current_table_name = table_name
        # if table_name not in self.partitioned_tables:
        #     logger.error("Table %s is not a partitioned table" % (table_name))
        #     logger.error(".... [ %s ]" %
        #                  " ".join(list({k: 1
        #                                 for k in self.partitioned_tables})))
        #     assert (0)

        sql_stmt = """ALTER TABLE {table_name} RECOVER PARTITIONS
        """.format(table_name=table_name)

    def describe_table(self, table_name):
        sql_stmt = """
        DESCRIBE TABLE {table_name} INTO {cos_out} STORED AS CSV""".format(
            table_name=table_name, cos_out=self.target_url)
        return self.execute_sql(sql_stmt, get_result=True)