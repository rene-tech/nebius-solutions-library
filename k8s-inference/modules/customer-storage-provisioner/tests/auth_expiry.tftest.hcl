mock_provider "nebius" {}

variables {
  project_id              = "project-customer-storage-test"
  resource_public_key_pem = <<-PEM
    -----BEGIN PUBLIC KEY-----
    MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA4C04Y+GkyY/deun2wssL
    iRafnyg94A4f7r2kQ2n05pHNyA2Dxyozs6ejVz+EesSNR6M4MNoatsePr5yjuiSQ
    36aAraLiEH7SPNp2maGEmcuZiZ/aIqLwThg9SN3WTwuVn95GCExKunBpOJAWfJnD
    YsJ4SS7IGxQv2K59/fvK7Io+jM2fpXznbaxJNRr8eWe2lMHD0mw7A6oE1Lz4vjAu
    WzLLmaoMUlYeFBLwhJP4K+h5W7y1kNcYn593e6i12kh8UhPFI2oWKJhnp18LWZCl
    FlkkqI6Mlfznv7msQ9lp03yVkJX/HNTI+wCjv28aC7ZiHBjJG90RElo/gycP2E+9
    IwIDAQAB
    -----END PUBLIC KEY-----
  PEM
  iam_public_key_pem      = <<-PEM
    -----BEGIN PUBLIC KEY-----
    MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA4C04Y+GkyY/deun2wssL
    iRafnyg94A4f7r2kQ2n05pHNyA2Dxyozs6ejVz+EesSNR6M4MNoatsePr5yjuiSQ
    36aAraLiEH7SPNp2maGEmcuZiZ/aIqLwThg9SN3WTwuVn95GCExKunBpOJAWfJnD
    YsJ4SS7IGxQv2K59/fvK7Io+jM2fpXznbaxJNRr8eWe2lMHD0mw7A6oE1Lz4vjAu
    WzLLmaoMUlYeFBLwhJP4K+h5W7y1kNcYn593e6i12kh8UhPFI2oWKJhnp18LWZCl
    FlkkqI6Mlfznv7msQ9lp03yVkJX/HNTI+wCjv28aC7ZiHBjJG90RElo/gycP2E+9
    IwIDAQAB
    -----END PUBLIC KEY-----
  PEM
}

run "accept_future_bounded_expiry" {
  command = plan

  variables {
    auth_key_expires_at = timeadd(timestamp(), "24h")
  }
}

run "reject_non_rfc3339_expiry" {
  command = plan

  variables {
    auth_key_expires_at = "not-a-timestamp"
  }

  expect_failures = [var.auth_key_expires_at]
}

run "reject_expired_auth_key" {
  command = plan

  variables {
    auth_key_expires_at = "2020-01-01T00:00:00Z"
  }

  expect_failures = [var.auth_key_expires_at]
}

run "reject_auth_key_beyond_maximum_lifetime" {
  command = plan

  variables {
    auth_key_expires_at = timeadd(timestamp(), "2161h")
  }

  expect_failures = [var.auth_key_expires_at]
}
