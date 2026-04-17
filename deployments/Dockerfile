FROM nginx:alpine

COPY nginx.conf /etc/nginx/templates/default.conf.template
COPY index.html /usr/share/nginx/html/index.html

# Only substitute GITHUB_TOKEN — leave nginx vars ($uri, $host, etc.) alone
ENV NGINX_ENVSUBST_FILTER=GITHUB_TOKEN

EXPOSE 3000
