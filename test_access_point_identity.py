from thanos.cad_debug import _element_attributes

class FakeDxf:
    def __init__(self, layer='WLAN AP'):
        self.layer = layer

class FakeAttr:
    def __init__(self, tag, text):
        self.dxf = type('D', (), {'tag': tag, 'text': text})()

class FakeInsert:
    def __init__(self, attrs):
        self.attribs = [FakeAttr(k, v) for k, v in attrs.items()]


def test_access_point_keeps_block_identity_not_hardware_type():
    entity = FakeInsert({'TYP': 'AP-535', 'NR': 'AP/3', 'SZAFA': 'R1'})
    misc, name, element_name = _element_attributes(entity, 'access_point')
    assert element_name == 'access_point'
    assert name == ''


def test_explicit_name_still_has_priority_for_access_point():
    entity = FakeInsert({'Name': 'Punkt WLAN', 'TYP': 'AP-535'})
    misc, name, element_name = _element_attributes(entity, 'access_point')
    assert element_name == 'Punkt WLAN'


def test_other_blocks_keep_legacy_type_naming():
    entity = FakeInsert({'TYP': 'AP-535'})
    misc, name, element_name = _element_attributes(entity, 'switch')
    assert element_name == 'AP-535'
