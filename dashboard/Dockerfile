FROM nginx:alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html /usr/share/nginx/html/index.html
COPY login.html /usr/share/nginx/html/login.html
COPY flags.html /usr/share/nginx/html/flags.html
COPY deployments.html /usr/share/nginx/html/deployments.html
COPY admin.html /usr/share/nginx/html/admin.html

EXPOSE 3000
