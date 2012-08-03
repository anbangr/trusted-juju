
from juju.lib.testing import TestCase
from juju.lib.pick import pick_key, pick_all_key, pick_attr, pick_all_attr


class adict(dict):
    def __getattr__(self, key):
        return self[key]

    def __setattr__(self, key, value):
        self[key] = value


class PickTest(TestCase):

    def setUp(self):
        self.sample_key_data = [{"role": "client", "name": "db"},
                                {"role": "server", "name": "website"},
                                {"role": "client", "name": "cache"}]

        self.sample_attr_data = [adict({"role": "client", "name": "db"}),
                                 adict({"role": "server", "name": "website"}),
                                 adict({"role": "client", "name": "cache"})]

    def test_pick_all_key(self):
        self.assertEqual(list(pick_all_key(self.sample_key_data, role="client")),
                         [{"role": "client", "name": "db"},
                          {"role": "client", "name": "cache"}])

    def test_pick_key(self):
        sd = self.sample_key_data
        self.assertEqual(pick_key(sd, role="client"),
                         {"role": "client", "name": "db"})

        self.assertEqual(pick_key(sd, role="server"),
                         {"role": "server", "name": "website"})

        self.assertEqual(pick_key(sd, role="client", name="db"),
                         {"role": "client", "name": "db"})

        self.assertEqual(pick_key(sd, role="client", name="foo"), None)

    def test_pick_all_attr(self):
        self.assertEqual(list(pick_all_attr(self.sample_attr_data, role="client")),
                         [{"role": "client", "name": "db"},
                          {"role": "client", "name": "cache"}])

    def test_pick_attr(self):
        sd = self.sample_attr_data
        self.assertEqual(pick_attr(sd, role="client"),
                         {"role": "client", "name": "db"})

        self.assertEqual(pick_attr(sd, role="server"),
                         {"role": "server", "name": "website"})

        self.assertEqual(pick_attr(sd, role="client", name="db"),
                         {"role": "client", "name": "db"})

        self.assertEqual(pick_attr(sd, role="client", name="foo"), None)
