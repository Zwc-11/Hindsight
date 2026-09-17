//! Presentation queries retain historical and rights scope. Operational metadata is explicitly current.
use crate::model::*;
use rusqlite::params;
use serde_json::{json, Value};

pub fn public_point(p: &Point, scope: &Scope) -> Result<Value> {
    let mut v = serde_json::to_value(p)?;
    if scope.mode == "reconstructed" {
        let o = v
            .as_object_mut()
            .ok_or_else(|| invalid("Point serialization"))?;
        for k in ["observed_at", "generation", "capture_id"] {
            o.remove(k);
        }
        if let Some(extras) = o.get_mut("extras").and_then(Value::as_object_mut) {
            for k in [
                "provider_lastupdated",
                "source_record",
                "source_row",
                "original_document_verified",
            ] {
                extras.remove(k);
            }
        }
        o.insert(
            "availability_note".into(),
            "Reconstructed publication-plus-policy time; actual acquisition metadata is withheld."
                .into(),
        );
    }
    Ok(v)
}
impl Store {
    pub fn overview(&self, scope: &Scope) -> Result<Value> {
        scope.validate()?;
        let c = self.connect()?;
        let query = if scope.mode == "observed" {
            "SELECT o.id,o.series_id,o.period_end,o.value,o.unit,o.observed_at,o.published_at,o.quality FROM observation_versions o JOIN series s ON s.id=o.series_id WHERE o.generation<=?1 AND o.observed_at<=?2 AND (o.published_at IS NULL OR o.published_at<=?2) ORDER BY o.observed_at DESC,o.period_end DESC LIMIT 32"
        } else {
            "SELECT o.id,o.series_id,o.period_end,o.value,o.unit,o.reconstructed_at,o.published_at,o.quality FROM observation_versions o JOIN series s ON s.id=o.series_id WHERE o.generation<=?1 AND o.reconstructed_at<=?2 AND o.published_at<=?2 ORDER BY o.reconstructed_at DESC,o.period_end DESC LIMIT 32"
        };
        let mut q = c.prepare(query)?;
        let rows=q.query_map(params![scope.snapshot,scope.as_of],|r|Ok(json!({"id":r.get::<_,String>(0)?,"series_id":r.get::<_,String>(1)?,"period_end":r.get::<_,String>(2)?,"value":r.get::<_,Option<f64>>(3)?,"unit":r.get::<_,String>(4)?,"available_at":r.get::<_,i64>(5)?,"published_at":r.get::<_,Option<i64>>(6)?,"quality":r.get::<_,String>(7)?})))?.collect::<std::result::Result<Vec<_>,_>>()?;
        let mut allowed = Vec::new();
        for mut row in rows {
            if let Ok(s) = self.series(row["series_id"].as_str().unwrap_or_default(), scope) {
                row["name"] = s.name.into();
                row["entity"] = s.entity.into();
                allowed.push(row);
            }
        }
        Ok(
            json!({"scope":scope,"changes":allowed,"note":"Recent eligible acquisitions/releases, not automated explanations of market moves."}),
        )
    }
    pub fn networks(&self, scope: &Scope, entity: &str) -> Result<Value> {
        scope.validate()?;
        let c = self.connect()?;
        let mut q=c.prepare("SELECT r.id,r.kind,r.from_entity,r.to_entity,r.known_at,r.payload FROM relationship_versions r JOIN source_documents d ON d.id=r.source_document_id JOIN captures c ON c.id=d.capture_id WHERE r.known_at<=?1 AND r.valid_from<=?1 AND (r.valid_to IS NULL OR r.valid_to>?1) AND r.status='active' AND (?2='' OR r.from_entity=?2 OR r.to_entity=?2) AND r.revision=(SELECT MAX(x.revision) FROM relationship_versions x WHERE x.id=r.id AND x.known_at<=?1) LIMIT 100")?;
        let rows=q.query_map(params![scope.as_of,entity],|r|Ok(json!({"id":r.get::<_,String>(0)?,"kind":r.get::<_,String>(1)?,"from":r.get::<_,String>(2)?,"to":r.get::<_,String>(3)?,"known_at":r.get::<_,i64>(4)?,"evidence":r.get::<_,String>(5)?})))?.collect::<std::result::Result<Vec<_>,_>>()?;
        // No graph builder currently publishes verified named-company edges. Reject unsupported paths rather than show guessed suppliers.
        if !rows.is_empty() {
            return Err(Error::Blocked(
                "Imported relationship generations/policies need verification before graph serving"
                    .into(),
            ));
        }
        Ok(
            json!({"scope":scope,"edges":[],"state":"insufficient_evidence","reason":"No source-verified, snapshot-qualified corporate relationships have been imported. Statistical associations are a separate layer."}),
        )
    }
    pub fn revisions(&self, id: &str, scope: &Scope) -> Result<Value> {
        self.series(id, scope)?;
        let c = self.connect()?;
        let mut q=c.prepare("SELECT id,period_start,period_end,value,unit,published_at,observed_at,reconstructed_at,source_order,quality FROM observation_versions WHERE series_id=?1 AND generation<=?2 AND ((?3='observed' AND observed_at<=?4 AND (published_at IS NULL OR published_at<=?4)) OR (?3='reconstructed' AND reconstructed_at<=?4 AND published_at<=?4)) ORDER BY period_end DESC,COALESCE(source_order,observed_at) DESC LIMIT 200")?;
        let rows=q.query_map(params![id,scope.snapshot,scope.mode,scope.as_of],|r|Ok(json!({"id":r.get::<_,String>(0)?,"period_start":r.get::<_,String>(1)?,"period_end":r.get::<_,String>(2)?,"value":r.get::<_,Option<f64>>(3)?,"unit":r.get::<_,String>(4)?,"published_at":r.get::<_,Option<i64>>(5)?,"available_at":if scope.mode=="observed"{Some(r.get::<_,i64>(6)?)}else{r.get::<_,Option<i64>>(7)?},"source_order":r.get::<_,Option<i64>>(8)?,"quality":r.get::<_,String>(9)?})))?.collect::<std::result::Result<Vec<_>,_>>()?;
        Ok(
            json!({"scope":scope,"rows":rows,"limit":200,"note":"Only captured eligible versions are shown; absence of earlier captures is not proof no historical revisions occurred."}),
        )
    }
}
