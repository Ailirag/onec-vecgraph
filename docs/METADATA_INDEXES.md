# Database indexing declarations (onec-lite 0.2.0+)

`get_metadata_indexes(kind, name, workspace, source='', table='', field='',
include_unindexed=False, offset=0, max_results=100)` is read-only, available in
full/lean/review. Reads current metadata without FTS rebuilds, Neo4j or SQL access.

Example: `get_metadata_indexes(kind='Document', name='ЗаявкаНаЗакупку', workspace='ut')`.
`include_unindexed=True` includes DontIndex and unspecified field properties.
`table` and `field` are case-insensitive substrings. Follow `next_offset` (limit 500).
Each row identifies source, file and XML line. Base and extension declarations are
separate; errors produce `complete=False`, never an empty successful index list.

## Rule map, platform 8.3.27

| Area | Evidence |
|---|---|
| Catalog, Document, ExchangePlan, charts, BusinessProcess, Task | Attribute and tabular attribute Indexing; standard code/number/presentation, hierarchy, owners |
| DocumentJournal | Column indexing; platform journal indexes |
| InformationRegister | Dimension/resource/attribute indexing; dimension order, periodicity, write mode, main filter, slice totals |
| Accumulation/Accounting/CalculationRegister | Dimension/resource/attribute flags and totals; platform recorder and auxiliary tables |
| CommonAttribute | Indexing/separation; actual usage needs separate object analysis |
| Sequence | Platform registration/boundary tables, additional index declarations |
| Enum, Constant | Platform storage indexes without a custom Indexing property |
| Forms, modules, reports etc. | No independent storage table |

Three layers must be distinguished:

1. Field `Indexing`: `DontIndex`, `Index`, `IndexWithAdditionalOrder`. Omitted
   properties stay unspecified, including EDT defaults and adopted fragments.
   `DontIndex` does not exclude a field from other/composite/platform indexes.
2. Additional indexes: Configurator `Object/Ext/AdditionalIndexes.xml`; EDT
   `Object/AdditionalIndexes.aindex`. Ordered key fields and included fields are
   separate; table is main, tabular or register virtual table. Since 8.3.26, supported
   storage objects can declare these indexes. CORP, usage settings and restructuring
   determine physical creation. ValueStorage/unlimited strings cannot be keys;
   composite types expand into several physical columns. An extension file is
   evidence, not proof that a platform version permits its use.
3. Automatic indexes: consult official per-kind rules with the returned object
   settings. Physical keys are deliberately not synthesized from incomplete metadata.

FullTextSearch is separate from database B-tree indexes. Metadata does not prove
deployment, SQL names, manually created SQL indexes or optimizer usage.

References: [platform index rules](https://its.1c.ru/db/content/metod8dev/src/admins/i8101590.htm),
[developer guide, 5.6.13 and 9.4](https://1c-dn.com/library/tutorials/1c_enterprise_developer_guide_8_3_27/).

## Rollout

The tool is lite-only: vector read/write/admin servers and Neo4j need no upgrade.
Clients with explicit tool allowlists must add `get_metadata_indexes`.
