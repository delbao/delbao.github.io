---
layout: post
title: Comment intégrer des scripts d'utilité externe dans Logstash Pipeline
comments: true
lang: fr
ref: pipeline
thumbnail: hackergotchi_big.png
summary: This article describes the approach to call external applications from Logstash Pipeline and use their (JSON/XML/key-value) output in the further filtering process.
tags: [logstash,elasticsearch,ruby]
---

### Aperçu

[Logstash](https://www.elastic.co/products/logstash) est un excellent outil pour traiter les journaux et en extraire des données précieuses. Il existe de nombreuses Logstash utiles
[filter plugins](https://www.elastic.co/guide/en/logstash/current/filter-plugins.html) qui facilitent le traitement des données de journal brut. Cependant, parfois
Les utilitaires externes sont nécessaires pour traiter les données de manière plus compliquée que les plugins de filtrage existants peuvent.

Il est possible de [code your own filter plugin](https://www.elastic.co/guide/en/logstash/current/_how_to_write_a_logstash_filter_plugin.html) dans Ruby
Mais que faire si vous avez déjà le filtre mis en œuvre dans un autre langage de programmation et que vous souhaitez le réutiliser dans Logstash?

Dans ce cas, il est plus facile de communiquer avec ce filtre externe à partir de Logstash. Cet article démontre la manière la plus simple d'intégrer les données externes

Applications dans le pipeline Logstash:

1. Logstash lance un programme externe et envoie les données d'entrée à travers les arguments de ligne de commande et stdin
1. Le programme externe écrit des résultats à stdout dans n'importe quel format compris par les filtres Logstash (par exemple, JSON)
1. Logstash analyse la sortie du programme externe et continue de le gérer dans le pipeline

Il est inutile de dire que ce n'est pas la meilleure approche en termes de performance.
E.g., si le temps de démarrage de l'application externe est significatif, vous pouvez considérer
Pour lancer cette application une fois (en tant que démon / service) et communiquer avec elle en utilisant [ØMQ] (http://en.wikipedia.org/wiki/%C3%98MQ) ou d'autres files d'attente de messages haute performance.

L'explication détaillée et l'exemple d'utilisation sont indiqués ci-dessous.



## Lancement du programme externe

Nous utiliserons [ruby filter] (https://www.elastic.co/guide/en/logstash/current/plugins-filters-ruby.html) afin de lancer une application externe et de capturer sa sortie:

{% highlight ruby %}
filter {
    # <...> <- More filters are above
    # Launching external script to make a deeper text analysis
    if [file_path] =~ /.+/ {
       ruby {
          code => 'require "open3"
                   body_path = event["file_path"]
                   cmd =  "/opt/bin/my_filter.py -f #{file_path}"
                   stdin, stdout, stderr = Open3.popen3(cmd)
                   event["process_result"] = stdout.read
                   err = stderr.read
                   if err.to_s.empty?
                     filter_matched(event)
                   else
                     event["ext_script_err_msg"] = err
                   end'
          remove_field => ["file_path"]
       }
    }
    # Parsing of the process_result is here (see the next section)
 }
{% endhighlight %}

Remarque:

+ L'application externe `` `/ opt / bin / my_filter.py``` est lancée uniquement si le champ` `` file_path``` n'est pas vide. Ce champ doit être extrait plus tôt dans le pipeline du filtre. C'est la valeur (`` `# {file_path}` ``) est utilisé dans
La ligne de commande pour lancer un filtre externe.
+ Stdin handle est accessible pour notre petit script ruby ​​et il peut être utilisé pour envoyer plus de données au programme externe (`` `/ opt / bin / my_filter.py```).
+ Si l'application stderr n'est pas vide, le filtre n'est pas considéré comme réussi et le contenu de stderr est enregistré dans le champ `` `ext_script_err_msg```.
+ Si le traitement a réussi, la sortie du programme externe est enregistrée dans `` `process_result``` déposé et le champ` `file_path``` est supprimé

### Analyse de la sortie du programme externe (JSON)

La manière la plus simple de remettre les données à Logstash consiste à utiliser l'un des formats de données structurés compris par les filtres Logstash: [JSON](https://www.elastic.co/guide/fr/logstash/current/plugins-filters- Json.html),
[XML](https://www.elastic.co/guide/en/logstash/current/plugins-filters-xml.html) ou plus ancien [key-value (kv)](https: // www. Elastic.co/guide/en/logstash/current/plugins-filters-kv.html).


Exemple avec JSON:

{% highlight ruby %}
  if [process_result] =~ /.+/ {
       json {
          source => "process_result"
          remove_field => [ "process_result" ]
       }
    }
{% endhighlight %}

Remarque:

+ Le champ `` `process_result``` contient la sortie de l'application externe et est censé être au format JSON.
+ Si l'analyse a été effectuée, les champs JSON deviennent des champs d'événement et le champ intermédiaire `` `process_result``` est supprimé.

### Plusieurs mots sur le plugin de sortie exec

Si vous avez seulement besoin de lancer un utilitaire externe lors d'un événement Logstash apparié, vous pouvez envisager d'utiliser une approche plus simple
- [exec output plugin](https://www.elastic.co/guide/fr/logstash/current/plugins-outputs-exec.html).

{% include twitter_plug.html %}
