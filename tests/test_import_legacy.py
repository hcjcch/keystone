# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2012 OpenStack LLC
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import os

try:
    import sqlite3 as dbapi
except ImportError:
    from pysqlite2 import dbapi2 as dbapi

from keystone import test

from keystone.catalog.backends import templated as catalog_templated
from keystone.common.sql import legacy
from keystone import config
from keystone import identity
from keystone.identity.backends import sql as identity_sql


CONF = config.CONF


class ImportLegacy(test.TestCase):
    def setUp(self):
        super(ImportLegacy, self).setUp()
        self.config([test.etcdir('keystone.conf.sample'),
                     test.testsdir('test_overrides.conf'),
                     test.testsdir('backend_sql.conf'),
                     test.testsdir('backend_sql_disk.conf')])
        test.setup_test_database()
        self.identity_man = identity.Manager()
        self.identity_api = identity_sql.Identity()

    def tearDown(self):
        test.teardown_test_database()
        super(ImportLegacy, self).tearDown()

    def setup_old_database(self, sql_dump):
        sql_path = test.testsdir(sql_dump)
        db_path = test.tmpdir('%s.db' % sql_dump)
        try:
            os.unlink(db_path)
        except OSError:
            pass
        script_str = open(sql_path).read().strip()
        conn = dbapi.connect(db_path)
        conn.executescript(script_str)
        conn.commit()
        return db_path

    def test_import_d5(self):
        db_path = self.setup_old_database('legacy_d5.sqlite')
        migration = legacy.LegacyMigration('sqlite:///%s' % db_path)
        migration.migrate_all()

        admin_id = '1'
        user_ref = self.identity_api.get_user(admin_id)
        self.assertEquals(user_ref['name'], 'admin')
        self.assertEquals(user_ref['enabled'], True)

        # check password hashing
        user_ref = self.identity_man.authenticate(
            user_id=admin_id, password='secrete')

        # check catalog
        self._check_catalog(migration)

    def test_import_diablo(self):
        db_path = self.setup_old_database('legacy_diablo.sqlite')
        migration = legacy.LegacyMigration('sqlite:///%s' % db_path)
        migration.migrate_all()

        admin_id = '1'
        user_ref = self.identity_api.get_user(admin_id)
        self.assertEquals(user_ref['name'], 'admin')
        self.assertEquals(user_ref['enabled'], True)

        # check password hashing
        user_ref = self.identity_man.authenticate(
            user_id=admin_id, password='secrete')

        # check catalog
        self._check_catalog(migration)

    def test_import_essex(self):
        db_path = self.setup_old_database('legacy_essex.sqlite')
        migration = legacy.LegacyMigration('sqlite:///%s' % db_path)
        migration.migrate_all()

        admin_id = 'c93b19ea3fa94484824213db8ac0afce'
        user_ref = self.identity_api.get_user(admin_id)
        self.assertEquals(user_ref['name'], 'admin')
        self.assertEquals(user_ref['enabled'], True)

        # check password hashing
        user_ref = self.identity_man.authenticate(
            user_id=admin_id, password='secrete')

        # check catalog
        self._check_catalog(migration)

    def _check_catalog(self, migration):
        catalog_lines = migration.dump_catalog()
        catalog = catalog_templated.parse_templates(catalog_lines)
        self.assert_('RegionOne' in catalog)
        self.assert_('compute' in catalog['RegionOne'])
        self.assert_('adminURL' in catalog['RegionOne']['compute'])
