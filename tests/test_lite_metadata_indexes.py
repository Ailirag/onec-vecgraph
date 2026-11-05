import pytest

from onec_vecgraph.lite.metadata_indexes import read_index_declarations
from onec_vecgraph.lite import server


def put(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = text.replace('<MetaDataObject>', '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">')
    path.write_text(text, encoding="utf-8")
    return path


def test_configurator_roles_and_tabular_not_confused(tmp_path):
    p = put(tmp_path / "Documents" / "Test.xml", '''<MetaDataObject><Document>
    <Properties><Name>Test</Name><NumberLength>11</NumberLength></Properties><ChildObjects>
    <Attribute><Properties><Name>A</Name><Indexing>IndexWithAdditionalOrder</Indexing>
    <FullTextSearch>Use</FullTextSearch></Properties></Attribute>
    <TabularSection><Properties><Name>Rows</Name></Properties><ChildObjects><Attribute>
    <Properties><Name>A</Name><Indexing>DontIndex</Indexing></Properties></Attribute>
    </ChildObjects></TabularSection></ChildObjects></Document></MetaDataObject>''')
    r = read_index_declarations(p, p.with_suffix(""), "Document", "Test")
    assert r["object_settings"] == {"NumberLength": "11"}
    assert [(f["table"], f["index_request"]) for f in r["entries"]] == [
        ("Document.Test", "index_with_additional_order"), ("Document.Test.Rows", "not_requested")]
    assert r["additional_indexes_state"] == "not_present_in_dump"


@pytest.mark.parametrize("edt", [False, True])
def test_additional_indexes_preserve_order_table_and_include(tmp_path, edt):
    obj = tmp_path / "Documents" / "Test"
    p = put(obj / "Test.mdo" if edt else obj.with_suffix(".xml"),
            '<Document><name>Test</name></Document>')
    put(obj / "AdditionalIndexes.aindex" if edt else obj / "Ext/AdditionalIndexes.xml",
        '''<a:AdditionalIndexes xmlns:a="urn:test"><indexes><id>uuid</id><name>ByRows</name>
        <table>Document.Test.Rows</table><indexedFields><path>B</path></indexedFields>
        <indexedFields><path>A</path></indexedFields><additionalFields><path>Ref</path></additionalFields>
        </indexes></a:AdditionalIndexes>''' if edt else '''<AdditionalIndexes><AdditionalIndex id="uuid">
        <Name>ByRows</Name><Table>Document.Test.Rows</Table><IndexedFields><Field>B</Field><Field>A</Field>
        </IndexedFields><AdditionalFields><Field>Ref</Field></AdditionalFields></AdditionalIndex></AdditionalIndexes>''')
    r = read_index_declarations(p, obj, "Document", "Test")["entries"][0]
    assert r["key_fields"] == ["B", "A"]
    assert r["included_fields"] == ["Ref"]
    assert r["table"] == "Document.Test.Rows"
    assert r["id"] == "uuid"
    assert r["physical_presence"] == "not_verified"


def test_edt_omitted_and_unknown_values_are_not_false(tmp_path):
    p = put(tmp_path / "Test.mdo", '''<InformationRegister><name>Test</name>
    <dimensions><name>A</name><mainFilter>true</mainFilter><indexing>FutureMode</indexing></dimensions>
    <dimensions><name>B</name></dimensions><resources><name>Value</name><indexing>Index</indexing></resources>
    <attributes><name>Remark</name><indexing>DontIndex</indexing></attributes></InformationRegister>''')
    r = read_index_declarations(p, tmp_path, "InformationRegister", "Test")
    assert r["dimension_order"] == ["A", "B"]
    assert [x["index_request"] for x in r["entries"]] == ["unknown", "unspecified", "index", "not_requested"]
    assert r["entries"][0]["settings"]["mainFilter"] == "true"


def test_broken_sidecar_is_error_not_no_indexes(tmp_path):
    p = put(tmp_path / "T.xml", "<Catalog><Name>T</Name></Catalog>")
    put(tmp_path / "T/Ext/AdditionalIndexes.xml", "<Broken/>")
    with pytest.raises(ValueError, match="Invalid additional"):
        read_index_declarations(p, tmp_path / "T", "Catalog", "T")


def test_external_entity_not_expanded(tmp_path):
    secret = put(tmp_path / "secret.txt", "DO_NOT_READ")
    p = put(tmp_path / "T.xml", f'''<!DOCTYPE Catalog [<!ENTITY xxe SYSTEM "{secret.as_uri()}">]>
    <Catalog><Name>T</Name><Attribute><Name>A</Name><Indexing>&xxe;</Indexing></Attribute></Catalog>''')
    assert "DO_NOT_READ" not in str(read_index_declarations(p, tmp_path, "Catalog", "T"))


def test_tool_source_provenance_filters_and_pagination(tmp_path, monkeypatch):
    from onec_vecgraph.lite.workspace import Workspace
    base = tmp_path / "base"
    ext = tmp_path / "ext"
    for root, extension in [(base, False), (ext, True)]:
        put(root / "Configuration.xml", '<MetaDataObject><Configuration><Properties><Name>'
            + ("Extension" if extension else "Base") + '</Name>'
            + ('<ConfigurationExtensionPurpose>Customization</ConfigurationExtensionPurpose>' if extension else '')
            + '</Properties></Configuration></MetaDataObject>')
        put(root / "Catalogs/T.xml", '<MetaDataObject><Catalog><Properties><Name>T</Name></Properties>'
            '<ChildObjects><Attribute><Properties><Name>A</Name><Indexing>Index</Indexing></Properties>'
            '</Attribute><Attribute><Properties><Name>B</Name><Indexing>DontIndex</Indexing></Properties>'
            '</Attribute></ChildObjects></Catalog></MetaDataObject>')
    ws = Workspace(base, (ext,))
    monkeypatch.setattr(server, "_ws", lambda _: ws)
    result = server.get_metadata_indexes("Catalog", "T", max_results=1)
    assert result["complete"] and result["total"] == 2 and result["next_offset"] == 1
    assert len(result["sources"]) == 2
    assert result["entries"][0]["source"] != server.get_metadata_indexes("Catalog", "T", offset=1)["entries"][0]["source"]
    assert server.get_metadata_indexes("Catalog", "T", source="Base", include_unindexed=True)["total"] == 2
    assert server.get_metadata_indexes("Catalog", "T", field="b")["total"] == 0
    assert server.get_metadata_indexes("Catalog", "T", field="b", include_unindexed=True)["total"] == 2
    assert "error" in server.get_metadata_indexes("Catalog", "T", max_results=501)
    assert "error" in server.get_metadata_indexes("Catalog", "Missing")


@pytest.mark.parametrize("profile", ["lean", "review", "full"])
def test_tool_in_all_profiles(monkeypatch, profile):
    monkeypatch.setenv("ONEC_LITE_PROFILE", profile)
    assert server._published("get_metadata_indexes")
