import os
import os.path
from BeautifulSoup import BeautifulSoup
from sqlalchemy.orm import subqueryload
from sqlalchemy import and_, or_, not_

import inpho.corpus.stats as dm
from inpho.model import Idea, Thinker, Entity, Session 

from inpho.corpus.terms import *
    

def extract_article_body(filename):
    f=open(filename)
    doc=f.read()
    soup=BeautifulSoup(doc)

    # rip out bibliography
    biblio_root = soup.findAll('h2', text='Bibliography')
    if biblio_root:
        biblio_root = biblio_root[-1].findParent('h2')
        biblio = [biblio_root]
        biblio.extend(biblio_root.findNextSiblings())
        biblio = [elm.extract() for elm in biblio]

    # grab modified body 
    body=soup.find("div", id="aueditable")

    return body.text
     

def process_article(article, terms=None, entity_type=Idea, output_filename=None,
                    corpus_root='corpus/'):
    if terms is None:
        terms = inpho_terms(entity_type)
    

    lines = []

    filename = os.path.join(corpus_root, article, 'index.html')
    article_terms = Session.query(entity_type)
    article_terms = article_terms.filter(entity_type.sep_dir==article)
    article_terms = article_terms.all()
    if filename and os.path.isfile(filename):
        print "processing:", article, filename
        try: 
            doc = extract_article_body(filename)
            lines = dm.prepare_apriori_input(doc, terms, article_terms)
        except:
            print "ERROR PROCESSING:", article, filename
    else:
        print "BAD SEP_DIR:", article

    if output_filename:
        with open(output_filename, 'w') as f:
            f.writelines(lines)
    else:
        return lines

from multiprocessing import Pool

def process_wrapper(args):
    return process_article(*args)

def process_articles(entity_type=Entity, output_filename='output-all.txt',
                     corpus_root='corpus/'):
    terms = inpho_terms(entity_type)
    
    articles = Session.query(Entity.sep_dir).filter(Entity.sep_dir!=None)
    articles = articles.filter(Entity.sep_dir!='')
    articles = articles.distinct().all()
    articles = [a[0] for a in articles]
   
    # parallel processing of articles
    p = Pool()
    args = [(title, terms, entity_type, None, corpus_root) for title in articles]
    doc_lines = p.map(process_wrapper, args)
    p.close()

    #serial processing for tests
    '''
    doc_lines = []
    for title in articles:
        lines = process_article(title, terms, entity_type, None, corpus_root)
        doc_lines.append(lines)
    '''

    # write graph output to file
    print output_filename
    with open(output_filename, 'w') as f:
        for lines in doc_lines:
            f.writelines(lines)

import subprocess
import beagle
def run_beagle(entity_type=Idea, filename='beagle.txt', root='./',
                    corpus_root='corpus/', d=64):
    output_filename = os.path.abspath(root + "beagle-" + filename)
   
    # select terms 
    terms = inpho_terms(entity_type)

    # build environment vectors
    env = beagle.build_env_vectors(terms, d)

    # process SEP articles for cooccurrence data
    articles = Session.query(Entity.sep_dir).filter(Entity.sep_dir!=None)
    articles = articles.filter(Entity.sep_dir!='')
    articles = articles.distinct().limit(10).all()
    articles = [a[0] for a in articles]

    corpus = []
    for article in articles:
        filename = os.path.join(corpus_root, article, 'index.html')
    
        if filename and os.path.isfile(filename):
            print "processing:", article, filename
            try: 
                doc = extract_article_body(filename)
                corpus.extend(dm.get_sentence_occurrences(doc, terms))
       
            except:
                print "ERROR PROCESSING:", filename
    
    # initialize context vector
    # we actually don't do this because during the loop over the memory vector
    
    memory = env.copy()
    #initialize memory vector with own environment vector
    for sentence in corpus:
        for word in sentence:
            memory[word] += sum([env[id] for id in sentence if id != word])            
            # add sentence vector

    return memory


def complete_mining(entity_type=Idea, filename='graph.txt', root='./',
                    corpus_root='corpus/', update_entropy=False):
    occur_filename = os.path.abspath(root + "graph-" + filename)
    edge_filename = os.path.abspath(root + "edge-" + filename)
    sql_filename = os.path.abspath(root + "sql-" + filename)


    print "processing articles..."
    process_articles(entity_type, occur_filename, corpus_root=corpus_root)

    print "running apriori miner..."
    dm.apriori(occur_filename, edge_filename)
    
    print "processing edges..."
    edges = dm.process_edges(occur_filename, edge_filename)
    ents = dm.calculate_node_entropy(edges)
    edges = dm.calculate_edge_weight(edges, ents)
    
    print "creating sql files..."

    with open(sql_filename, 'w') as f:
        for edge, props in edges.iteritems():
            ante,cons = edge
            row = "%s::%s" % edge
            row += "::%(confidence)s::%(jweight)s::%(weight)s\n" % props
            f.write(row)

    print "updating term entropy..."

    if update_entropy:
        for term_id, entropy in ents.iteritems():
            term = Session.query(Idea).get(term_id)
            if term:
                term.entropy = entropy

        Session.flush()
        Session.commit()
        Session.close()

    update_graph(entity_type, sql_filename)

def update_graph(entity_type, sql_filename):
    # Import SQL statements
    if entity_type == Idea:
        table = "idea_graph_edges"
    elif entity_type == Thinker:
        table = "thinker_graph_edges"
    else:
        table = "idea_thinker_graph_edges"

    connection = Session.connection()

    print "deleting old graph information ..."
    connection.execute("""
    DELETE FROM %(table)s;
    """ % {'filename' : sql_filename, 'table' : table })
    
    print "inserting new graph information"
    connection.execute("""
    SET foreign_key_checks=0;
    LOAD DATA INFILE '%(filename)s'
    INTO TABLE %(table)s
    FIELDS TERMINATED BY '::'
    (ante_id, cons_id, confidence, jweight, weight);
    SET foreign_key_checks=1;
    """ % {'filename' : sql_filename, 'table' : table })
    Session.close()


if __name__ == "__main__":
    from ConfigParser import ConfigParser
    config = ConfigParser()
    config.read('sql.ini')
    corpus_root = config.get('corpus', 'path') 

    from optparse import OptionParser

    usage = "usage: %prog [options] config_file"
    parser = OptionParser(usage)
    parser.set_defaults(type='all', mode='complete', update_entropy=False)
    parser.add_option("-a", "--all", action="store_const",
                      dest='type', const='all',
                      help="mine all edges [default]")
    parser.add_option("-i", "--idea", action="store_const",
                      dest='type', const='idea',
                      help="mine only idea-idea edges")
    parser.add_option("-t", "--thinker", action="store_const",
                      dest='type', const='thinker',
                      help="mine only thinker-thinker edges")
    parser.add_option("--complete", action="store_const",
                      dest='mode', const='complete',
                      help="complete data mining process [default]")
    parser.add_option("--no-entropy", action="store_const",
                      dest='mode', const='no_entropy',
                      help="data mining, skipping update of entropy scores")
    parser.add_option("--load", action="store_const",
                      dest='mode', const='load',
                      help="load data from sql files")
    parser.add_option("-b", "--beagle", action="store_const",
                      dest='mode', const='beagle',
                      help="run the BEAGLE model")
    options, args = parser.parse_args()

    filename_root = options.type

    entity_type = Entity
    if options.type == 'idea':
        entity_type = Idea
    elif options.type == 'thinker':
        entity_type = Thinker

    if options.mode == 'complete':
        complete_mining(entity_type, 
                        filename=filename_root, 
                        corpus_root=corpus_root, 
                        update_entropy=options.update_entropy)
    elif options.mode == 'no_entropy':
        complete_mining(entity_type, 
                        filename=filename_root, 
                        corpus_root=corpus_root, 
                        update_entropy=False)
    elif options.mode == 'load':
        sql_filename = os.path.abspath(corpus_root + "sql-" + filename_root)
        update_graph(entity_type, sql_filename)
    elif options.mode == 'beagle':
        env = run_beagle(entity_type, filename=filename_root, corpus_root=corpus_root)
        print env
