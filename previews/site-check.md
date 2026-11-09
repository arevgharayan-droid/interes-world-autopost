## Проверка сайта https://peakproductions.co

- /wp-json/: HTTP 200 · application/json; charset=UTF-8 · сайт «peakproductions.co» · namespaces: oembed/1.0, hostinger-easy-onboarding/v1, litespeed/v1, litespeed/v3, v1, hostinger-amplitude/v1, hostinger-reach/v1, hostinger-tools-plugin/v1, google-site-kit/v1, ad-inserter/v1, wp/v2, wp-site-health/v1, wp-block-editor/v1, wp-abilities/v1 · auth: application-passwords · '{"name":"peakproductions.co","description":"","url":"https:\\/\\/peakproductions.co","home":"https:\\/\\/peakproductions.co"'
- /wp-json/wp/v2/users/me: HTTP 401 · application/json; charset=UTF-8 · '{"code":"rest_not_logged_in","message":"You are not currently logged in.","data":{"status":401}}'
- /wp-json/wp/v2/posts?per_page=1: HTTP 200 · application/json; charset=UTF-8 · '[{"id":1,"date":"2026-11-06T09:31:10","date_gmt":"2026-11-06T09:31:10","guid":{"rendered":"http:\\/\\/peakproductions.co\\/'
- /?rest_route=/: HTTP 200 · application/json; charset=UTF-8 · '{"name":"peakproductions.co","description":"","url":"https:\\/\\/peakproductions.co","home":"https:\\/\\/peakproductions.co"'
- авторизация (пробный несуществующий логин): HTTP 401 · rest_not_logged_in
- авторизация (секреты WP_USER + WP_APP_PASSWORD): HTTP 401 · rest_not_logged_in

Расшифровка: invalid_username — такого логина на сайте нет; incorrect_password — пароль приложения неверный; rest_not_logged_in — заголовок авторизации не доходит до WordPress.
