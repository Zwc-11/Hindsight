#pragma once
#include "rust/cxx.h"
#include <cstddef>
namespace hindsight {
rust::Vec<double> ridge_solve(rust::Slice<const double> x, rust::Slice<const double> y, std::size_t columns, double penalty);
}
