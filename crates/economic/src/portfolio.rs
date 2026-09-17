use crate::market::Market;
use crate::types::*;
use serde::Serialize;
use std::collections::BTreeSet;

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct LedgerEvent {
    pub order_id: u64,
    pub at: Time,
    pub phase: String,
    pub shares: i64,
    pub price_micros: i64,
    pub fee_micros: i64,
    pub cash_micros: i64,
    pub position: i64,
    pub reason: String,
}
#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct Receivable {
    pub id: String,
    pub pay_at: Time,
    pub amount_micros: i64,
}
#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct Account {
    pub cash_micros: i64,
    pub shares: i64,
    pub fees_micros: i64,
    pub traded_notional_micros: i64,
    pub events: Vec<LedgerEvent>,
    pub receivables: Vec<Receivable>,
    pub applied_actions: BTreeSet<String>,
}
fn exact(v: i128) -> Result<i64> {
    i64::try_from(v).map_err(|_| "fixed-decimal account overflow".into())
}
fn charge(notional: i64, bps: u32) -> Result<i64> {
    exact((i128::from(notional) * i128::from(bps) + 9999) / 10000)
}
impl Account {
    pub fn new(cash: i64) -> Self {
        Self {
            cash_micros: cash,
            shares: 0,
            fees_micros: 0,
            traded_notional_micros: 0,
            events: vec![],
            receivables: vec![],
            applied_actions: BTreeSet::new(),
        }
    }
    pub fn equity(&self, mark: i64) -> Result<i64> {
        exact(
            i128::from(self.cash_micros)
                + i128::from(self.shares) * i128::from(mark)
                + self
                    .receivables
                    .iter()
                    .map(|r| i128::from(r.amount_micros))
                    .sum::<i128>(),
        )
    }
    // Explicit fields mirror the immutable ledger schema; avoid opaque positional tuples.
    #[allow(clippy::too_many_arguments)]
    pub fn event(
        &mut self,
        id: u64,
        at: Time,
        phase: &str,
        shares: i64,
        price: i64,
        fee: i64,
        reason: &str,
    ) {
        self.events.push(LedgerEvent {
            order_id: id,
            at,
            phase: phase.into(),
            shares,
            price_micros: price,
            fee_micros: fee,
            cash_micros: self.cash_micros,
            position: self.shares,
            reason: reason.into(),
        });
    }
    pub fn apply_actions(&mut self, data: &Dataset, symbol: &str, at: Time) -> Result<()> {
        let mut actions: Vec<_> = data
            .corporate_actions
            .iter()
            .filter(|a| a.symbol == symbol && a.effective_at <= at)
            .collect();
        actions.sort_by_key(|a| (a.effective_at, &a.id));
        for a in actions {
            if self.applied_actions.contains(&a.id) {
                continue;
            }
            let split = i128::from(self.shares) * i128::from(a.split_numerator);
            if split % i128::from(a.split_denominator) != 0 {
                return Err(
                    "fractional split requires explicit cash-in-lieu data; unsupported".into(),
                );
            }
            let amount = exact(i128::from(self.shares) * i128::from(a.dividend_per_share_micros))?;
            if amount > 0 {
                let pay = a.dividend_pay_at.ok_or("dividend payable date missing")?;
                if pay < a.effective_at {
                    return Err("dividend pay date precedes entitlement".into());
                }
                self.receivables.push(Receivable {
                    id: a.id.clone(),
                    pay_at: pay,
                    amount_micros: amount,
                });
            }
            self.shares = exact(split / i128::from(a.split_denominator))?;
            self.applied_actions.insert(a.id.clone());
            self.event(
                0,
                a.effective_at,
                "corporate_action",
                self.shares,
                0,
                0,
                &a.id,
            );
        }
        let due: i128 = self
            .receivables
            .iter()
            .filter(|r| r.pay_at <= at)
            .map(|r| i128::from(r.amount_micros))
            .sum();
        if due > 0 {
            self.cash_micros = exact(i128::from(self.cash_micros) + due)?;
            self.event(
                0,
                at,
                "dividend_payment",
                0,
                0,
                0,
                "cash credited only after payable date",
            );
            self.receivables.retain(|r| r.pay_at > at);
        }
        Ok(())
    }
    pub fn rebalance(
        &mut self,
        target: i64,
        decision: Time,
        id: u64,
        c: &Config,
        market: &Market<'_>,
    ) -> Result<()> {
        if id == 0 || self.events.iter().any(|e| e.order_id == id) {
            return Err("duplicate or reserved order id".into());
        }
        if target < 0 || target > c.max_shares {
            return Err("target outside long-only risk limit".into());
        }
        self.event(
            id,
            decision,
            "decision",
            target,
            0,
            0,
            "frozen model target; not a live order",
        );
        let desired = target - self.shares;
        if desired == 0 {
            self.event(id, decision, "no_order", 0, 0, 0, "already at target");
            return Ok(());
        }
        let visible = market.observed(&c.symbol, decision, c.observation_delay_ms);
        let Some(last) = visible.last() else {
            self.event(id, decision, "rejected", desired, 0, 0, "no observed price");
            return Ok(());
        };
        if decision - last.end > c.max_quote_age_ms + c.observation_delay_ms {
            self.event(
                id,
                decision,
                "rejected",
                desired,
                0,
                0,
                "stale observed price",
            );
            return Ok(());
        }
        // Capacity is fixed from already-completed volume, never the future fill minute.
        let cap = exact(i128::from(last.volume) * i128::from(c.max_past_volume_ppm) / 1_000_000)?;
        let size = desired.abs().min(cap);
        if size == 0 {
            self.event(
                id,
                decision,
                "rejected",
                desired,
                0,
                0,
                "zero past-volume capacity",
            );
            return Ok(());
        }
        let arrival = decision
            .checked_add(c.order_latency_ms)
            .ok_or("arrival overflow")?;
        self.event(
            id,
            decision,
            "submitted",
            desired.signum() * size,
            0,
            0,
            "bounded market-order simulation",
        );
        self.event(
            id,
            arrival,
            "arrived",
            desired.signum() * size,
            0,
            0,
            "declared order latency",
        );
        let Some(reference) = market.fill_reference(&c.symbol, arrival) else {
            self.event(
                id,
                arrival,
                "cancelled",
                desired,
                0,
                0,
                "missing next eligible minute; never skip to a favorable bar",
            );
            return Ok(());
        };
        let half_spread_impact = u64::from(c.spread_bps) + 2 * u64::from(c.impact_bps);
        let adjustment =
            exact((i128::from(reference.open) * i128::from(half_spread_impact) + 19999) / 20000)?;
        let price = reference
            .open
            .checked_add(desired.signum() * adjustment)
            .ok_or("price overflow")?;
        if price <= 0 {
            return Err("nonpositive modeled price".into());
        }
        let mut executable = size;
        if desired > 0 {
            let unit_with_fee = price
                .checked_add(charge(price, c.fee_bps)?)
                .ok_or("unit price overflow")?;
            executable = executable.min(self.cash_micros / unit_with_fee);
        }
        if executable == 0 {
            self.event(
                id,
                reference.start,
                "cancelled",
                0,
                price,
                0,
                "cash limit at contemporary execution price",
            );
            return Ok(());
        }
        let notional = exact(i128::from(executable) * i128::from(price))?;
        let fee = charge(notional, c.fee_bps)?;
        let signed = desired.signum() * executable;
        let new_cash = exact(
            i128::from(self.cash_micros) - i128::from(signed) * i128::from(price) - i128::from(fee),
        )?;
        let new_shares = self.shares.checked_add(signed).ok_or("position overflow")?;
        let new_fees = self.fees_micros.checked_add(fee).ok_or("fee overflow")?;
        let new_notional = self
            .traded_notional_micros
            .checked_add(notional)
            .ok_or("turnover overflow")?;
        if new_cash < 0 || new_shares < 0 {
            return Err("account invariant violation".into());
        }
        self.cash_micros = new_cash;
        self.shares = new_shares;
        self.fees_micros = new_fees;
        self.traded_notional_micros = new_notional;
        self.event(id,reference.start,"simulated_fill",signed,price,fee,"future minute OPEN reference; modeled half-spread+impact, not an observed executable quote");
        self.event(
            id,
            reference.start + c.report_latency_ms,
            "report_received",
            signed,
            price,
            fee,
            "strategy learns fill after report latency",
        );
        if executable < desired.abs() {
            self.event(
                id,
                reference.start + c.report_latency_ms,
                "remainder_cancelled",
                desired - signed,
                0,
                0,
                "cash or past-volume cap; no hidden retry",
            );
        }
        Ok(())
    }
}
