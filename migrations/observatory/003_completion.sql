UPDATE jobs SET state='validated',complete_through=NULL
WHERE state='complete_for_declared_scope'
AND kind IN ('census_catalog','census_totals','census_data','bea_catalog','bea_data','issuer_catalog','sec_document','alpaca_bars');
