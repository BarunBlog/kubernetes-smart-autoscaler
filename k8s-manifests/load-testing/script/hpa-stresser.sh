kubectl run omni-stresser -n development --image=busybox --restart=Never -- /bin/sh -c \
"while true; do \
  wget --post-data='' -q -O- <YOUR-ALB-DNS-NAME>/order; \
  echo ' Order Placed'; \
done"