import unittest
from datetime import datetime
from unittest.mock import Mock, patch

import pandas as pd

from helper_functions import (
    NemPointQueryError,
    add_settlement_columns,
    add_time_columns,
    aggregate_to_half_hour,
    aggregate_generation_to_half_hour,
    apply_fuel_mapping,
    average_availability_profile,
    average_demand_by_half_hour,
    build_availability_query,
    build_demand_query,
    build_generation_profile,
    build_generation_query,
    build_price_query,
    broken_axis_limits,
    build_time_series_quality_report,
    calculate_gas_weighted_price,
    calculate_generation_delta,
    calculate_spreads,
    daily_average_availability,
    half_hour_time_labels,
    qt_post_request,
    summarise_availability,
)


class QueryBuilderTests(unittest.TestCase):
    def test_price_query_uses_inclusive_end(self):
        query = build_price_query(datetime(2026, 8, 1), datetime(2026, 9, 1))
        self.assertIn("SETTLEMENTDATE <= '2026-09-01 00:00:00'", query)

    def test_generation_query_uses_exclusive_end_and_state(self):
        query = build_generation_query(
            "NSW1", datetime(2026, 8, 1), datetime(2026, 9, 1)
        )
        self.assertIn("Region = 'NSW1'", query)
        self.assertIn("SETTLEMENTDATE < '2026-09-01 00:00:00'", query)

    def test_demand_query_uses_exclusive_end(self):
        query = build_demand_query(datetime(2026, 8, 1), datetime(2026, 9, 1))
        self.assertIn("SETTLEMENTDATE < '2026-09-01 00:00:00'", query)


    def test_availability_query_uses_dispatchload_availability(self):
        query = build_availability_query(
            "NSW1", datetime(2026, 8, 1), datetime(2026, 9, 1)
        )
        self.assertIn("Region = 'NSW1'", query)
        self.assertIn("dl.AVAILABILITY", query)
        self.assertIn("dl.INTERVENTION = 0", query)
        self.assertIn("SETTLEMENTDATE < '2026-09-01 00:00:00'", query)


class RequestTests(unittest.TestCase):
    @patch("helper_functions.requests.post")
    def test_request_passes_credentials_timeout_and_validates_columns(self, post):
        response = Mock()
        response.text = "REGIONID,VALUE\nNSW1,1\n"
        post.return_value = response

        result = qt_post_request(
            "select 1",
            api_key="test-key",
            url="https://example.test/query",
            timeout=12,
            required_columns=("REGIONID", "VALUE"),
        )

        post.assert_called_once_with(
            "https://example.test/query",
            params={"query": "select 1", "key": "test-key", "format": "csv"},
            timeout=12,
        )
        pd.testing.assert_frame_equal(result, pd.DataFrame({"REGIONID": ["NSW1"], "VALUE": [1]}))

    @patch("helper_functions.requests.post")
    def test_request_reports_missing_columns(self, post):
        response = Mock()
        response.text = "REGIONID\nNSW1\n"
        post.return_value = response

        with self.assertRaises(NemPointQueryError):
            qt_post_request(
                "select 1",
                api_key="test-key",
                required_columns=("REGIONID", "VALUE"),
            )


class TimeSeriesTests(unittest.TestCase):
    def test_time_columns_include_fiscal_year_and_interval(self):
        data = pd.DataFrame({'SETTLEMENTDATE': ["2026-07-01 00:05:00"]})
        result = add_time_columns(data)
        self.assertEqual(result.loc[0, 'FY'], 2027)
        self.assertEqual(result.loc[0, 'Interval'], 1)

    def test_half_hour_aggregation_is_right_closed(self):
        data = pd.DataFrame({
            'Fuel': ['Gas', 'Gas'],
            'Region': ['NSW1', 'NSW1'],
            'SETTLEMENTDATE': ["2026-08-01 00:05:00", "2026-08-01 00:30:00"],
            'Generation': [10.0, 20.0],
        })
        result = aggregate_to_half_hour(data, 'Generation', ['Fuel', 'Region'])
        self.assertEqual(result['HH'].tolist(), [1])
        self.assertEqual(result['Generation'].tolist(), [15.0])

    def test_quality_report_identifies_duplicate_and_incomplete_day(self):
        data = pd.DataFrame({
            'REGIONID': ['NSW1', 'NSW1'],
            'SETTLEMENTDATE': ["2026-08-01 00:05:00", "2026-08-01 00:05:00"],
        })
        report = build_time_series_quality_report(data, group_columns=['REGIONID'])
        self.assertEqual(report['duplicate_key_rows'], 1)
        self.assertEqual(len(report['incomplete_days']), 1)

    def test_half_hour_time_labels_map_to_clock_times(self):
        labels = half_hour_time_labels([0, 1, 47])
        self.assertEqual(labels[0].strftime('%H:%M'), '00:00')
        self.assertEqual(labels[1].strftime('%H:%M'), '00:30')
        self.assertEqual(labels[2].strftime('%H:%M'), '23:30')


class SpreadTests(unittest.TestCase):
    def test_one_hour_spread_uses_complete_day_and_top_bottom_intervals(self):
        timestamps = pd.date_range(
            '2026-08-01 00:05:00', periods=288, freq='5min'
        )
        prices = pd.DataFrame({'NSW1': 0.0}, index=timestamps)
        prices.iloc[0, 0] = -10.0
        prices.iloc[-1, 0] = 100.0

        period, daily, diagnostics = calculate_spreads(
            prices,
            durations=[1],
            start_periods=[datetime(2026, 8, 1).date()],
            end_periods=[datetime(2026, 8, 2).date()],
        )

        self.assertAlmostEqual(daily.loc['2026-08-01', 'NSW1.1'], 55 / 6)
        self.assertAlmostEqual(period.loc[datetime(2026, 8, 1).date(), 'NSW1.1'], 55 / 6)
        self.assertEqual(diagnostics['excluded_days'], [])

    def test_incomplete_day_is_excluded_and_reported(self):
        prices = pd.DataFrame(
            {'NSW1': [10.0]},
            index=pd.DatetimeIndex(['2026-08-01 00:05:00']),
        )
        _, daily, diagnostics = calculate_spreads(
            prices,
            durations=[1],
            start_periods=[datetime(2026, 8, 1).date()],
            end_periods=[datetime(2026, 8, 2).date()],
        )

        self.assertTrue(daily.empty)
        self.assertEqual(diagnostics['valid_day_count'], 0)
        self.assertEqual(len(diagnostics['excluded_days']), 1)


class GenerationTests(unittest.TestCase):
    def test_generation_profile_and_half_hour_aggregation(self):
        data = pd.DataFrame({
            'Fuel': ['Gas', 'Gas'],
            'Region': ['NSW1', 'NSW1'],
            'SETTLEMENTDATE': ['2026-08-01 00:05:00', '2026-08-01 00:30:00'],
            'Generation': [10.0, 20.0],
        })
        profile = build_generation_profile(data, 'NSW1')
        half_hour = aggregate_generation_to_half_hour(data)
        self.assertEqual(profile.loc[1, 'Gas'], 10.0)
        self.assertEqual(half_hour.loc[0, 'Generation'], 15.0)

    def test_fuel_mapping_retains_unmapped_rows(self):
        generation = pd.DataFrame({'Fuel': ['Gas', 'Hydro'], 'Generation': [1, 2]})
        mapping = pd.DataFrame({'NeoPoint Tag': ['Gas'], 'Mapped Fuel': ['Gas']})
        mapped, unmapped = apply_fuel_mapping(generation, mapping)
        self.assertEqual(len(mapped), 2)
        self.assertEqual(unmapped, ['Hydro'])
        self.assertTrue(pd.isna(mapped.loc[1, 'Mapped Fuel']))

    def test_gas_weighted_price_ignores_non_positive_generation(self):
        data = pd.DataFrame({
            'Price': [100.0, 200.0, 999.0],
            'Generation': [1.0, 3.0, 0.0],
        })
        self.assertEqual(calculate_gas_weighted_price(data), 175.0)

    def test_generation_delta_uses_full_half_hour_grid_and_missing_values_zero(self):
        current = pd.DataFrame({
            'HH': [0, 0, 1],
            'Fuel': ['Black Coal', 'Gas', 'Black Coal'],
            'Generation': [100.0, 50.0, 110.0],
        })
        previous = pd.DataFrame({
            'HH': [0, 1],
            'Fuel': ['Black Coal', 'Gas'],
            'Generation': [90.0, 45.0],
        })
        result = calculate_generation_delta(current, previous)
        self.assertAlmostEqual(result.loc[0, 'Black Coal'], 10.0)
        self.assertAlmostEqual(result.loc[0, 'Gas'], 50.0)
        self.assertAlmostEqual(result.loc[1, 'Gas'], -45.0)
        self.assertAlmostEqual(result.loc[1, 'Black Coal'], 0.0)


class DemandTests(unittest.TestCase):
    def test_average_demand_by_half_hour(self):
        data = pd.DataFrame({
            'REGIONID': ['NSW1', 'NSW1'],
            'SETTLEMENTDATE': ['2026-08-01 00:05:00', '2026-08-01 00:25:00'],
            'TOTALDEMAND': [100.0, 120.0],
        })
        result = average_demand_by_half_hour(data)
        self.assertEqual(result.loc[0, 'HH'], 0)
        self.assertEqual(result.loc[0, 'TOTALDEMAND'], 110.0)


class AvailabilityTests(unittest.TestCase):
    @staticmethod
    def _frame(values, start='2026-08-01 00:05:00'):
        times = pd.date_range(start, periods=len(values), freq='5min')
        return pd.DataFrame({
            'Fuel': 'Black Coal',
            'Region': 'NSW1',
            'SETTLEMENTDATE': times,
            'Availability': values,
        })

    def test_settlement_columns_assign_midnight_to_previous_day(self):
        data = pd.DataFrame({
            'SETTLEMENTDATE': ['2026-08-02 00:00:00', '2026-08-01 00:05:00'],
        })
        result = add_settlement_columns(data)
        self.assertEqual(result.loc[0, 'SettlementDay'], pd.Timestamp('2026-08-01'))
        self.assertEqual(result.loc[0, 'SettlementPeriod'], 48)
        self.assertEqual(result.loc[1, 'SettlementPeriod'], 1)

    def test_average_profile_covers_48_periods(self):
        result = average_availability_profile(self._frame([100.0, 200.0]))
        self.assertEqual(len(result), 48)
        self.assertEqual(result.loc[1, 'Black Coal'], 150.0)
        self.assertTrue(pd.isna(result.loc[2, 'Black Coal']))

    def test_daily_average_availability(self):
        data = pd.concat([
            self._frame([100.0, 200.0]),
            self._frame([300.0], start='2026-08-02 12:00:00'),
        ])
        result = daily_average_availability(data)
        self.assertEqual(result.loc[pd.Timestamp('2026-08-01'), 'Black Coal'], 150.0)
        self.assertEqual(result.loc[pd.Timestamp('2026-08-02'), 'Black Coal'], 300.0)

    def test_summary_changes_and_zero_baseline(self):
        summary = summarise_availability(
            self._frame([90.0]), self._frame([100.0]), self._frame([0.0])
        )
        row = summary.loc['Black Coal']
        self.assertEqual(row['Change vs previous month (MW)'], -10.0)
        self.assertAlmostEqual(row['Change vs previous month (%)'], -10.0)
        self.assertTrue(pd.isna(row['Change vs previous year (%)']))
        self.assertAlmostEqual(row['% of peak'], 90.0)


class BrokenAxisLimitTests(unittest.TestCase):
    def test_extreme_values_trigger_break(self):
        values = [80, 90, 100, 110, 120, 95, 85, 105, 1129, 6182]
        limits = broken_axis_limits(values)
        self.assertIsNotNone(limits)
        (low_min, low_max), (high_min, high_max) = limits
        self.assertEqual(low_min, 0.0)
        self.assertGreaterEqual(low_max, 120)
        self.assertLess(low_max, 1129)
        self.assertLess(high_min, 1129)
        self.assertGreater(high_min, low_max)
        self.assertGreater(high_min, 0)
        self.assertGreater(high_max, 6182)

    def test_mild_tail_has_no_break(self):
        values = [80, 90, 100, 110, 120, 95, 85, 105, 150, 190]
        self.assertIsNone(broken_axis_limits(values))

    def test_no_outliers_has_no_break(self):
        self.assertIsNone(broken_axis_limits([90, 100, 110, 120]))


if __name__ == "__main__":
    unittest.main()
