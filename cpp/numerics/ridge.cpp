#include "ridge.hpp"
#include <algorithm>
#include <cmath>
#include <limits>
#include <stdexcept>
#include <vector>
namespace hindsight {
// Small regularized system with partial pivoting. Column zero is an intercept.
rust::Vec<double> ridge_solve(rust::Slice<const double> x, rust::Slice<const double> y,
                             std::size_t columns, double penalty) {
    if (columns == 0 || columns > 64 || y.size() < 2 ||
        y.size() > std::numeric_limits<std::size_t>::max() / columns ||
        x.size() != y.size() * columns || !std::isfinite(penalty) || penalty <= 0)
        throw std::invalid_argument("invalid ridge dimensions/penalty");
    std::vector<double> a(columns * columns, 0), b(columns, 0);
    for (std::size_t r = 0; r < y.size(); ++r) {
        if (!std::isfinite(y[r])) throw std::invalid_argument("nonfinite target");
        for (std::size_t j = 0; j < columns; ++j) {
            if (!std::isfinite(x[r*columns+j])) throw std::invalid_argument("nonfinite feature");
            b[j] += x[r*columns+j] * y[r];
            for (std::size_t k = 0; k < columns; ++k)
                a[j*columns+k] += x[r*columns+j] * x[r*columns+k];
        }
    }
    for (std::size_t j = 1; j < columns; ++j) a[j*columns+j] += penalty;
    for (std::size_t j = 0; j < columns; ++j) {
        std::size_t pivot = j;
        for (std::size_t k = j+1; k < columns; ++k)
            if (std::abs(a[k*columns+j]) > std::abs(a[pivot*columns+j])) pivot = k;
        if (!std::isfinite(a[pivot*columns+j]) || std::abs(a[pivot*columns+j]) < 1e-14)
            throw std::runtime_error("singular/ill-conditioned ridge system");
        for (std::size_t k = 0; k < columns; ++k) std::swap(a[j*columns+k], a[pivot*columns+k]);
        std::swap(b[j], b[pivot]);
        for (std::size_t k = j+1; k < columns; ++k) {
            const double ratio = a[k*columns+j] / a[j*columns+j];
            for (std::size_t p = j; p < columns; ++p) a[k*columns+p] -= ratio*a[j*columns+p];
            b[k] -= ratio*b[j];
        }
    }
    rust::Vec<double> weights;
    for (std::size_t j = 0; j < columns; ++j) weights.push_back(0);
    for (std::size_t j = columns; j-- > 0;) {
        double value = b[j];
        for (std::size_t k = j+1; k < columns; ++k) value -= a[j*columns+k]*weights[k];
        weights[j] = value/a[j*columns+j];
        if (!std::isfinite(weights[j])) throw std::runtime_error("nonfinite coefficient");
    }
    return weights;
}
}
