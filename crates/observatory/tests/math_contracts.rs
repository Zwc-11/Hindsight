use hindsight_observatory::{
    analysis,
    math::{self, covariance, dependence, state_space::*},
    model::*,
};
use serde_json::json;
fn close(a: f64, b: f64) {
    assert!((a - b).abs() < 1e-10, "{a} != {b}");
}
#[test]
fn correlation_identity_and_sign() {
    close(
        math::pearson(&[1., 2., 3., 4.], &[2., 4., 6., 8.]).unwrap(),
        1.,
    );
    close(
        math::pearson(&[1., 2., 3., 4.], &[8., 6., 4., 2.]).unwrap(),
        -1.,
    );
}
#[test]
fn correlation_rejects_constants_and_nonfinite() {
    assert!(math::pearson(&[1.; 5], &[2.; 5]).is_err());
    assert!(math::pearson(&[1., f64::NAN, 3.], &[1., 2., 3.]).is_err());
}
#[test]
fn correlation_respects_small_and_large_scales() {
    close(
        math::pearson(&[1e-200, 2e-200, 3e-200], &[3e200, 2e200, 1e200]).unwrap(),
        -1.,
    );
}
#[test]
fn ranks_average_ties() {
    assert_eq!(
        math::ranks(&[3., 1., 1., 2.]).unwrap(),
        vec![4., 1.5, 1.5, 3.]
    );
}
#[test]
fn spearman_matches_hand_ranked_result() {
    close(
        math::spearman(&[1., 2., 2., 4.], &[1., 3., 2., 4.]).unwrap(),
        0.9486832980505138,
    );
}
#[test]
fn nonlinear_dependence_without_linear_direction() {
    let x: Vec<_> = (-50..=50).map(|i| i as f64 / 50.).collect();
    let y: Vec<_> = x.iter().map(|v| v * v).collect();
    assert!(math::pearson(&x, &y).unwrap().abs() < 1e-12);
    assert!(dependence::distance_correlation(&x, &y).unwrap() > 0.45);
}
#[test]
fn distance_correlation_identity_constant_and_budget() {
    close(
        dependence::distance_correlation(&[1., 2., 3.], &[1., 2., 3.]).unwrap(),
        1.,
    );
    close(
        dependence::distance_correlation(&[1.; 4], &[1., 2., 3., 4.]).unwrap(),
        0.,
    );
    assert!(dependence::distance_correlation(&vec![1.; 2001], &vec![1.; 2001]).is_err());
}
#[test]
fn bh_by_match_small_known_family() {
    let p = [0.01, 0.04, 0.03, 0.2];
    let bh = dependence::adjust(&p, "BH").unwrap();
    for (a, b) in bh.iter().zip([0.04, 0.0533333333333, 0.0533333333333, 0.2]) {
        close(*a, b);
    }
    let by = dependence::adjust(&p, "BY").unwrap();
    for (a, b) in by.iter().zip(&bh) {
        close(*a, (b * 25. / 12.).min(1.));
    }
}
#[test]
fn invalid_probability_and_unknown_method_rejected() {
    assert!(dependence::adjust(&[-0.1], "BH").is_err());
    assert!(dependence::adjust(&[f64::NAN], "BY").is_err());
    assert!(dependence::adjust(&[0.5], "none").is_err());
}
#[test]
fn rolling_never_uses_later_window() {
    let x = [1., 2., 3., -10., -20.];
    let y = [1., 2., 3., 10., 20.];
    let r = dependence::rolling(&x, &y, 3).unwrap();
    close(r[0].pearson.unwrap(), 1.);
    assert_eq!(r[0].end, 3);
    assert!(r.last().unwrap().pearson.unwrap() < 0.);
}
#[test]
fn covariance_is_symmetric_and_partial_diagonal() {
    let rows = vec![
        vec![1., 2.],
        vec![2., 5.],
        vec![4., 3.],
        vec![3., 8.],
        vec![5., 7.],
    ];
    let c = covariance::ledoit_wolf(&rows).unwrap();
    assert!((0.0..=1.0).contains(&c.shrinkage));
    close(c.covariance[0][1], c.covariance[1][0]);
    close(c.partial[0][0].unwrap(), 1.);
    covariance::cholesky(&c.covariance).unwrap();
}
#[test]
fn covariance_shrinkage_can_support_more_columns_than_rows() {
    let rows = (0..5)
        .map(|i| (0..12).map(|j| ((i * 13 + j * 7) as f64).sin()).collect())
        .collect::<Vec<_>>();
    let c = covariance::ledoit_wolf(&rows).unwrap();
    assert!(c.shrinkage > 0.);
    assert_eq!(c.covariance.len(), 12);
}
#[test]
fn covariance_rejects_invalid_shapes_and_zero_variance() {
    assert!(covariance::ledoit_wolf(&[vec![1.], vec![1.], vec![1.]]).is_err());
    assert!(covariance::ledoit_wolf(&[vec![1.], vec![2., 3.], vec![3.]]).is_err());
}
fn filter_input() -> FilterInput {
    FilterInput {
        phi: 0.8,
        process_variance: 0.2,
        initial_variance: 1.,
        memory: 10,
        steps: 12,
        releases: vec![AggregateRelease {
            released_at_step: 5,
            period_start_step: 2,
            period_end_step: 4,
            value: 3.,
            aggregation: "sum".into(),
            noise_variance: 0.3,
        }],
    }
}
#[test]
fn aggregate_release_updates_only_at_publication() {
    let i = filter_input();
    let result = filter(&i).unwrap();
    for row in &result[..5] {
        close(row.mean, 0.);
    }
    assert!(result[5].mean > 0.);
    assert_eq!(result[5].observations_used, 1);
}
#[test]
fn future_filter_observation_does_not_change_past_states() {
    let i = filter_input();
    let a = filter(&i).unwrap();
    let mut j = i.clone();
    j.releases.push(AggregateRelease {
        released_at_step: 10,
        period_start_step: 9,
        period_end_step: 9,
        value: 1e6,
        aggregation: "last".into(),
        noise_variance: 0.5,
    });
    let b = filter(&j).unwrap();
    for k in 0..10 {
        close(a[k].mean, b[k].mean);
        close(a[k].variance, b[k].variance);
    }
}
#[test]
fn sum_and_average_loadings_are_not_interchanged() {
    let a = filter(&filter_input()).unwrap();
    let mut i = filter_input();
    i.releases[0].aggregation = "mean".into();
    let b = filter(&i).unwrap();
    assert!((a[5].mean - b[5].mean).abs() > 0.1);
}
#[test]
fn filter_rejects_prepublication_and_unsupported_memory() {
    let mut i = filter_input();
    i.releases[0].released_at_step = 3;
    assert!(filter(&i).is_err());
    let mut i = filter_input();
    i.memory = 2;
    assert!(filter(&i).is_err());
}
fn point(i: i32, value: Option<f64>) -> Point {
    Point {
        id: format!("p{i}"),
        key: format!("k{i}"),
        series_id: "s".into(),
        period_start: format!("{i}-01-01"),
        period_end: format!("{i}-12-31"),
        value,
        unit: "test".into(),
        published_at: None,
        observed_at: 0,
        reconstructed_at: None,
        source_order: None,
        capture_id: "c".into(),
        precision: "unknown".into(),
        quality: "synthetic".into(),
        generation: 1,
        extras: json!({}),
    }
}
#[test]
fn alignment_does_not_forward_fill_missing_measurements() {
    let x = vec![
        point(2000, Some(1.)),
        point(2001, None),
        point(2002, Some(4.)),
        point(2003, Some(8.)),
    ];
    let y = vec![
        point(2000, Some(2.)),
        point(2001, Some(3.)),
        point(2002, Some(8.)),
        point(2003, Some(9.)),
    ];
    let result = analysis::align(&[x, y], "change").unwrap();
    assert_eq!(result.len(), 1);
    assert_eq!(result[0].period_end, "2003-12-31");
}
#[test]
fn alignment_rejects_ambiguous_measurement_context() {
    let x = vec![point(2000, Some(1.)), point(2000, Some(2.))];
    assert!(analysis::align(&[x.clone(), x], "level").is_err());
}
#[test]
fn shift_null_states_its_limited_invariance_assumptions() {
    let x = (0..40).map(|i| (i as f64).sin()).collect::<Vec<_>>();
    let r = dependence::circular_shift_null(&x, &x, false).unwrap();
    assert!(r.p_value >= 1. / 40.);
    assert!(r.limitations.contains("Exploratory"));
}
