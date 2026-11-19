# Constrói a Calculadora RTC (regime regular) a partir do repositório oficial da Receita Federal.
# Fixe RTC_REF numa tag/commit para ter resultados reproduzíveis.
FROM maven:3.9-eclipse-temurin-21 AS build
ARG RTC_REF=main
RUN git clone --depth 1 --branch ${RTC_REF} https://github.com/ReceitaFederal/calculadora-rtc-regime-regular.git /src
WORKDIR /src
RUN mkdir -p calculadora/db \
 && mvn -q flyway:migrate -Dflyway.configFiles=./flyway/flyway-pro.conf --batch-mode --errors \
 && mvn -q clean package -DskipTests --batch-mode

FROM eclipse-temurin:21-jre
WORKDIR /app
COPY --from=build /src/target/api-regime-geral.jar ./api-regime-geral.jar
COPY --from=build /src/calculadora/db ./calculadora/db
EXPOSE 8080
ENTRYPOINT ["java", "-Dspring.profiles.active=offline", "-jar", "api-regime-geral.jar"]
