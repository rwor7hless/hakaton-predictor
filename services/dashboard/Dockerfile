FROM nginx:alpine

COPY services/dashboard/nginx.conf /etc/nginx/conf.d/default.conf
COPY services/dashboard/index.html /usr/share/nginx/html/index.html
COPY services/dashboard/css /usr/share/nginx/html/css
COPY services/dashboard/js /usr/share/nginx/html/js
COPY services/dashboard/vendor /usr/share/nginx/html/vendor
COPY services/dashboard/mock /usr/share/nginx/html/mock

RUN mkdir -p /usr/share/nginx/html/docs-site \
 && printf '<!doctype html><html lang="ru"><meta charset="utf-8"><title>Документация</title><p>Документация ещё не собрана: смонтируйте сборку Sphinx в /usr/share/nginx/html/docs-site.</p></html>\n' > /usr/share/nginx/html/docs-site/index.html

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=3s --retries=3 CMD wget -qO- http://127.0.0.1:8080/healthz >/dev/null || exit 1
