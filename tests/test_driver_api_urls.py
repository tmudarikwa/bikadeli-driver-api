from django.test import Client, TestCase
from django.urls import Resolver404, resolve


class DriverApiUrlTests(TestCase):
    urlconf = "driver_api.urls"

    def test_owned_driver_routes_resolve_locally(self):
        self.assertEqual(
            resolve("/api/v1/drivers/profile/me/", urlconf=self.urlconf).url_name,
            "driver-profile-me",
        )
        self.assertEqual(
            resolve(
                "/api/v1/drivers/profile/toggle_online/", urlconf=self.urlconf
            ).url_name,
            "driver-profile-toggle-online",
        )
        self.assertEqual(
            resolve("/api/v1/drivers/location/update/", urlconf=self.urlconf).url_name,
            "driver-location-update",
        )
        self.assertEqual(
            resolve(
                "/api/v1/drivers/location/history/",
                urlconf=self.urlconf,
            ).url_name,
            "driver-location-history",
        )
        self.assertEqual(
            resolve(
                "/api/v1/drivers/support-tickets/",
                urlconf=self.urlconf,
            ).url_name,
            "driver-support-tickets",
        )
        self.assertEqual(
            resolve(
                "/api/v1/drivers/support-tickets/42/reply/",
                urlconf=self.urlconf,
            ).url_name,
            "driver-support-ticket-reply",
        )
        self.assertEqual(
            resolve(
                "/api/v1/drivers/auth/register/", urlconf=self.urlconf
            ).url_name,
            "driver-register",
        )
        self.assertEqual(
            resolve(
                "/api/v1/drivers/profile/submit_verification/",
                urlconf=self.urlconf,
            ).url_name,
            "driver-kyc-documents",
        )
        self.assertEqual(
            resolve(
                "/api/v1/drivers/parcel-earnings/summary/",
                urlconf=self.urlconf,
            ).url_name,
            "driver-earnings-finance-proxy",
        )
        self.assertEqual(
            resolve("/api/v1/drivers/earnings/summary/", urlconf=self.urlconf).url_name,
            "driver-earnings-finance-proxy",
        )

    def test_orders_owned_driver_routes_use_orders_service_proxy(self):
        self.assertEqual(
            resolve("/api/v1/drivers/orders/available/", urlconf=self.urlconf).url_name,
            "driver-food-orders-service-proxy",
        )
        self.assertEqual(
            resolve("/api/v1/drivers/orders/42/accept/", urlconf=self.urlconf).url_name,
            "driver-food-orders-service-proxy",
        )
        self.assertEqual(
            resolve(
                "/api/v1/drivers/orders/42/upload-delivery-photo/",
                urlconf=self.urlconf,
            ).url_name,
            "driver-food-orders-service-proxy",
        )

    def test_unowned_driver_routes_retain_compatibility_proxy(self):
        self.assertEqual(
            resolve("/api/v1/auth/login/", urlconf=self.urlconf).url_name,
            "identity-service-proxy",
        )
        self.assertEqual(
            resolve("/api/v1/auth/logout-all/", urlconf=self.urlconf).url_name,
            "identity-service-proxy",
        )
        with self.assertRaises(Resolver404):
            resolve("/api/v1/vendors/profile/", urlconf=self.urlconf)

    def test_health_endpoints(self):
        client = Client()
        self.assertEqual(client.get("/health/").status_code, 200)
        self.assertEqual(client.get("/health/ready/").status_code, 200)
